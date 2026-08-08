from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, Optional

import sounddevice as sd

from xfyun_client import (
    DeepSeekTranslator,
    detect_source_language,
    lookup_manuscript_cache,
    store_manuscript_cache,
)

SAMPLE_RATE = 16000
BLOCK_FRAMES = 640  # 40ms @ 16kHz / 16bit / mono = 1280 bytes

# Azure 语音翻译输出格式：16kHz 16bit 单声道 PCM，与本地 TTS 播放器一致。
AZURE_OUTPUT_FORMAT = "raw-16khz-16bit-mono-pcm"

DEFAULT_VOICES = {
    "zh-CN": "zh-CN-XiaoxiaoNeural",
    "en-US": "en-US-AriaNeural",
}


def _load_speech() -> Any:
    try:
        import azure.cognitiveservices.speech as speech
    except ImportError as exc:
        raise RuntimeError(
            "缺少 Azure Speech SDK，请先安装：pip install azure-cognitiveservices-speech"
        ) from exc
    return speech


class AzureInterpreter:
    """麦克风 -> Azure AI Speech 实时语音翻译。

    使用欧洲区域（如 westeurope / northeurope）时，冰岛往返延迟远低于
    直连国内节点。事件接口与 XfyunInterpreter 保持一致，可在 app.py
    中无缝切换，无需改动 UI 层。
    """

    def __init__(
        self,
        azure_key: str,
        azure_region: str,
        on_event: Callable[[dict], None],
        *,
        direction: str = "zh_en",
        azure_voice_zh: str = DEFAULT_VOICES["zh-CN"],
        azure_voice_en: str = DEFAULT_VOICES["en-US"],
        glossary_entries: Optional[list[list[str]]] = None,
        reference_text: str = "",
        translation_interval_mode: str = "adaptive",
        play_tts: bool = False,
        input_device: Optional[int] = None,
        session_id: int = 0,
        initial_segments: Optional[list[dict]] = None,
        deepseek_api_key: str = "",
        deepseek_model: str = "deepseek-v4-flash",
    ) -> None:
        self.azure_key = str(azure_key or "").strip()
        self.azure_region = str(azure_region or "").strip()
        self.on_event = on_event
        self.direction = direction if direction in {"auto", "zh_en", "en_zh"} else "zh_en"
        self.voices = {
            "zh-CN": str(azure_voice_zh or DEFAULT_VOICES["zh-CN"]).strip(),
            "en-US": str(azure_voice_en or DEFAULT_VOICES["en-US"]).strip(),
        }
        # 复用讯飞客户端的术语表替换逻辑，不需要 DeepSeek API Key。
        self._glossary = DeepSeekTranslator(
            "", "", glossary_entries=glossary_entries, reference_text=""
        )
        self.play_tts = bool(play_tts)
        self.input_device = input_device
        self.session_id = int(session_id)
        self._initial_segments = [dict(segment) for segment in (initial_segments or [])]

        self._running = threading.Event()
        self._recognizer: Any = None
        self._audio_stream: Any = None
        self._mic_stream: Any = None
        self._tts_stream: Any = None
        self._tts_queue: queue.Queue[bytes] = queue.Queue(maxsize=240)

        self._ordered_segments: list[dict[str, Any]] = []
        self._ordered_lock = threading.RLock()
        self._segment_seq = 0
        self._interim_cn = ""
        self._interim_en = ""
        self._final_cn: list[str] = []
        self._final_en: list[str] = []
        self._current_source_language = "en" if self.direction == "en_zh" else "zh"

    # ---------- 公共接口（与 XfyunInterpreter 对齐） ----------

    @staticmethod
    def list_input_devices() -> list[tuple[int, str]]:
        devices: list[tuple[int, str]] = []
        for index, device in enumerate(sd.query_devices()):
            if int(device.get("max_input_channels", 0)) > 0:
                devices.append((index, str(device.get("name", f"Input {index}"))))
        return devices

    def start(self) -> None:
        if self._running.is_set():
            return
        if not self.azure_key or not self.azure_region:
            raise ValueError("Azure 密钥（Key）和区域（Region）不能为空")

        speech = _load_speech()
        self._speech = speech
        self._reset_state()

        cfg = speech.translation.SpeechTranslationConfig(
            subscription=self.azure_key, region=self.azure_region
        )
        cfg.set_property(
            speech.PropertyId.SpeechServiceConnection_SynthOutputFormat,
            AZURE_OUTPUT_FORMAT,
        )
        self._audio_stream = speech.audio.PushAudioInputStream()
        audio_config = speech.audio.AudioConfig(stream=self._audio_stream)

        if self.direction == "auto":
            # 自动模式：中英混合识别，双向都作为目标语言，按检测结果取反向译文。
            # 默认 LanguageIdMode=AtStart 会在会话开始时锁定源语言，中途切换
            # 语言不会重新识别；这里显式开启连续语言识别（Continuous LID）。
            cfg.set_property(
                speech.PropertyId.SpeechServiceConnection_LanguageIdMode,
                "Continuous",
            )
            cfg.add_target_language("zh-CN")
            cfg.add_target_language("en-US")
            try:
                auto_detect_cls = speech.languageconfig.AutoDetectSourceLanguageConfig
            except AttributeError:
                from azure.cognitiveservices.speech.languageconfig import (
                    AutoDetectSourceLanguageConfig as auto_detect_cls,
                )
            auto_cfg = auto_detect_cls(languages=["zh-CN", "en-US"])
            recognizer = speech.translation.TranslationRecognizer(
                translation_config=cfg,
                auto_detect_source_language_config=auto_cfg,
                audio_config=audio_config,
            )
        else:
            source, target = self._language_pair()
            cfg.speech_recognition_language = source
            cfg.add_target_language(target)
            if self.play_tts:
                cfg.set_property(
                    speech.PropertyId.SpeechServiceConnection_TranslationVoice,
                    self.voices[target],
                )
            recognizer = speech.translation.TranslationRecognizer(
                translation_config=cfg, audio_config=audio_config
            )

        recognizer.recognizing.connect(self._on_recognizing)
        recognizer.recognized.connect(self._on_recognized)
        recognizer.synthesizing.connect(self._on_synthesizing)
        recognizer.canceled.connect(self._on_canceled)
        recognizer.session_started.connect(self._on_session_started)
        recognizer.session_stopped.connect(self._on_session_stopped)
        self._recognizer = recognizer

        self._running.set()
        try:
            self._start_microphone()
            recognizer.start_continuous_recognition()
        except Exception as exc:
            self._running.clear()
            self._stop_recognizer()
            self._cleanup()
            raise RuntimeError(f"Azure 语音服务启动失败：{exc}") from exc
        self._emit("status", text="正在连接 Azure 语音服务…", state="connecting")

    def stop(self) -> None:
        if not self._running.is_set():
            return
        self._running.clear()
        self._emit("status", text="正在停止…", state="stopping")
        self._stop_recognizer()
        self._cleanup()

    def set_play_tts(self, enabled: bool) -> None:
        self.play_tts = bool(enabled)
        # Azure 的合成音色在会话启动时配置，重新开始会话后生效。

    def set_translation_interval_mode(self, mode: str) -> None:
        # Azure 实时翻译持续返回，无需按间隔重译；保留接口以兼容 UI。
        pass

    def update_translation_materials(self, glossary_entries, reference_text: str) -> None:
        self._glossary.set_context(glossary_entries or [], "")

    def finalize_session(self, timeout: float = 20.0) -> dict[str, Any]:
        self.stop()
        deadline = time.monotonic() + max(5.0, timeout)
        time.sleep(min(1.2, max(0.0, deadline - time.monotonic())))

        # 停止时若还有未落段的临时字幕，补成正式段落。
        pending_source = (
            self._interim_cn.strip()
            if self._current_source_language == "zh"
            else self._interim_en.strip()
        )
        pending_translation = (
            self._interim_en.strip()
            if self._current_source_language == "zh"
            else self._interim_cn.strip()
        )
        if pending_source:
            snapshot = self._segment_snapshot()
            last_source = str(snapshot[-1].get("source", "")).strip() if snapshot else ""
            if pending_source != last_source:
                self._append_ordered_segment(
                    pending_source,
                    pending_translation,
                    self._current_source_language,
                    status="complete" if pending_translation else "pending",
                )
                self._interim_cn = ""
                self._interim_en = ""

        segments = self._segment_snapshot()
        chinese_lines, english_lines = [], []
        for segment in segments:
            source = str(segment.get("source", "")).strip()
            translation = str(segment.get("translation", "")).strip()
            if str(segment.get("source_language", "zh")) == "en":
                if translation:
                    chinese_lines.append(translation)
                if source:
                    english_lines.append(source)
            else:
                if source:
                    chinese_lines.append(source)
                if translation:
                    english_lines.append(translation)
        completed = sum(1 for s in segments if str(s.get("translation", "")).strip())
        self._emit(
            "finalize_progress",
            completed=completed,
            total=len(segments),
            text=f"正在整理传译文档：{completed}/{len(segments)}",
        )
        return {
            "segments": segments,
            "chinese": "\n".join(chinese_lines),
            "english": "\n".join(english_lines),
            "completed": completed,
            "total": len(segments),
        }

    # ---------- 内部实现 ----------

    def _language_pair(self) -> tuple[str, str]:
        if self.direction == "zh_en":
            return "zh-CN", "en-US"
        return "en-US", "zh-CN"

    def _reset_state(self) -> None:
        self._ordered_segments = [dict(segment) for segment in self._initial_segments]
        self._segment_seq = len(self._ordered_segments)
        self._interim_cn = ""
        self._interim_en = ""
        self._final_cn = []
        self._final_en = []
        self._current_source_language = "en" if self.direction == "en_zh" else "zh"

    def _emit(self, event_type: str, **data: object) -> None:
        self.on_event({"type": event_type, "session_id": self.session_id, **data})

    def _start_microphone(self) -> None:
        def callback(indata: bytes, _frames: int, _time_info: object, status: object) -> None:
            if status:
                self._emit("status", text=f"音频提示：{status}", state="warning")
            if not self._running.is_set() or self._audio_stream is None:
                return
            try:
                self._audio_stream.write(bytes(indata))
            except Exception:
                pass

        self._mic_stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_FRAMES,
            device=self.input_device,
            channels=1,
            dtype="int16",
            callback=callback,
        )
        self._mic_stream.start()

    def _close_microphone(self) -> None:
        if self._mic_stream is not None:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except Exception:
                pass
            self._mic_stream = None

    def _stop_recognizer(self) -> None:
        recognizer = self._recognizer
        self._recognizer = None
        if recognizer is None:
            return
        try:
            recognizer.stop_continuous_recognition()
        except Exception:
            pass
        try:
            recognizer.close()
        except Exception:
            pass

    def _cleanup(self) -> None:
        self._close_microphone()
        self._close_tts()
        if self._audio_stream is not None:
            try:
                self._audio_stream.close()
            except Exception:
                pass
            self._audio_stream = None

    def _pick_source_translation(
        self, text: str, translations: dict, result: Any
    ) -> tuple[str, str]:
        """返回 (source_language, translation)，source_language 为 zh / en。"""
        if self.direction == "zh_en":
            return "zh", str(translations.get("en-US", "") or "").strip()
        if self.direction == "en_zh":
            return "en", str(translations.get("zh-CN", "") or "").strip()
        # auto：优先用 Azure 自动检测结果，缺失时退回本地启发式判断。
        detected = ""
        if getattr(self, "_speech", None) is not None:
            try:
                detected = str(
                    result.properties.get(
                        self._speech.PropertyId.SpeechServiceConnection_AutoDetectSourceLanguageResult
                    )
                    or ""
                )
            except Exception:
                detected = ""
        if not detected:
            detected = detect_source_language(text)
        if str(detected).startswith("zh"):
            return "zh", str(translations.get("en-US", "") or "").strip()
        return "en", str(translations.get("zh-CN", "") or "").strip()

    def _on_recognizing(self, evt: Any) -> None:
        if not self._running.is_set():
            return
        result = evt.result
        text = str(getattr(result, "text", "") or "")
        if not text.strip():
            return
        translations = dict(getattr(result, "translations", {}) or {})
        source_lang, translation = self._pick_source_translation(text, translations, result)
        with self._ordered_lock:
            self._current_source_language = source_lang
            if source_lang == "en":
                self._interim_en = text.strip()
                self._interim_cn = translation
            else:
                self._interim_cn = text.strip()
                self._interim_en = translation
        self._emit_subtitles()

    def _on_recognized(self, evt: Any) -> None:
        if not self._running.is_set():
            return
        result = evt.result
        text = str(getattr(result, "text", "") or "")
        if not text.strip():
            return
        translations = dict(getattr(result, "translations", {}) or {})
        source_lang, translation = self._pick_source_translation(text, translations, result)
        translation = self._glossary._apply_glossary(text, translation)
        target_lang = "zh-CN" if source_lang == "en" else "en-US"
        cached = lookup_manuscript_cache(text, target_lang)
        if cached:
            translation = cached
        elif translation:
            store_manuscript_cache(text, target_lang, translation)

        with self._ordered_lock:
            self._current_source_language = source_lang
            self._interim_cn = ""
            self._interim_en = ""
        if source_lang == "en":
            self._final_en.append(text)
            if translation:
                self._final_cn.append(translation)
        else:
            self._final_cn.append(text)
            if translation:
                self._final_en.append(translation)
        self._append_ordered_segment(
            text,
            translation,
            source_lang,
            status="complete" if translation else "pending",
        )
        self._emit_subtitles()

    def _on_synthesizing(self, evt: Any) -> None:
        if not self.play_tts or not self._running.is_set():
            return
        audio = None
        try:
            audio = evt.result.get_audio()
        except Exception:
            try:
                audio = evt.result.audio
            except Exception:
                audio = None
        if not audio:
            return
        if self._tts_stream is None:
            self._start_tts_player()
        try:
            self._tts_queue.put_nowait(bytes(audio))
        except queue.Full:
            pass

    def _on_session_started(self, _evt: Any) -> None:
        self._emit("status", text="已连接 Azure，正在监听麦克风", state="connected")

    def _on_session_stopped(self, _evt: Any) -> None:
        if not self._running.is_set():
            return
        self._running.clear()
        self._cleanup()
        self._emit("closed", text="Azure 连接已结束")

    def _on_canceled(self, evt: Any) -> None:
        details = str(getattr(evt, "error_details", "") or "")
        self._running.clear()
        self._stop_recognizer()
        self._cleanup()
        if details:
            self._emit("error", text=f"Azure 连接错误：{details}")
        self._emit("closed", text="Azure 连接已结束")

    def _append_ordered_segment(
        self,
        source: str,
        translation: str,
        source_language: str,
        *,
        status: str | None = None,
    ) -> int:
        source = source.strip()
        translation = translation.strip()
        with self._ordered_lock:
            self._segment_seq += 1
            self._ordered_segments.append(
                {
                    "id": f"{self._segment_seq:04d}",
                    "source": source,
                    "translation": translation,
                    "source_language": source_language,
                    "status": status or ("complete" if translation else "pending"),
                    "error": "",
                }
            )
            return len(self._ordered_segments) - 1

    def _segment_snapshot(self) -> list[dict[str, Any]]:
        with self._ordered_lock:
            return [dict(segment) for segment in self._ordered_segments]

    def _emit_subtitles(self) -> None:
        with self._ordered_lock:
            source_parts = [
                str(segment.get("source", "")).strip()
                for segment in self._ordered_segments
                if str(segment.get("source", "")).strip()
            ]
            translation_parts = [
                str(segment.get("translation", "")).strip()
                for segment in self._ordered_segments
                if str(segment.get("translation", "")).strip()
            ]
            interim_source = (
                self._interim_cn.strip()
                if self._current_source_language == "zh"
                else self._interim_en.strip()
            )
            interim_translation = (
                self._interim_en.strip()
                if self._current_source_language == "zh"
                else self._interim_cn.strip()
            )
            if interim_source:
                source_parts.append(interim_source)
            if interim_translation:
                translation_parts.append(interim_translation)
            segments = [dict(segment) for segment in self._ordered_segments]
        chinese = "\n".join(self._final_cn + ([self._interim_cn] if self._interim_cn else []))
        english = "\n".join(self._final_en + ([self._interim_en] if self._interim_en else []))
        self._emit(
            "subtitles",
            chinese=chinese,
            english=english,
            source_language=self._current_source_language,
            source_transcript="\n".join(source_parts),
            translation_transcript="\n".join(translation_parts),
            segments=segments,
        )

    # ---------- TTS 播放（与讯飞客户端一致，16kHz PCM） ----------

    def _start_tts_player(self) -> None:
        if self._tts_stream is not None:
            return
        self._tts_stream = sd.RawOutputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
        )
        self._tts_stream.start()
        threading.Thread(target=self._tts_loop, daemon=True).start()

    def _tts_loop(self) -> None:
        while self._running.is_set() or not self._tts_queue.empty():
            try:
                chunk = self._tts_queue.get(timeout=0.3)
            except queue.Empty:
                continue
            if self._tts_stream is not None:
                try:
                    self._tts_stream.write(chunk)
                except Exception as exc:
                    self._emit("error", text=f"播放译文语音失败：{exc}")
                    break

    def _close_tts(self) -> None:
        if self._tts_stream is not None:
            try:
                self._tts_stream.stop()
                self._tts_stream.close()
            except Exception:
                pass
            self._tts_stream = None
