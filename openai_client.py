from __future__ import annotations

import io
import json
import platform
import queue
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path
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

TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"
CHAT_URL = "https://api.openai.com/v1/chat/completions"

# 分段识别参数（秒）。音频攒够最短长度后才送识别；尾部静音或到达最长
# 长度时落段并翻译。
SEGMENT_MIN_SECONDS = 3.0
SEGMENT_MAX_SECONDS = 9.0
SILENCE_END_SECONDS = 0.55
SILENCE_RMS_THRESHOLD = 380.0
# 缓冲整体峰值低于该值视为无语音，不送识别，避免模型回显提示词。
SPEECH_PEAK_THRESHOLD = 900
# 最小请求间隔：只防连发，不做硬限速；真撞 429 时由重试兜底。
# （充值后账号限流会自动放宽，硬限速反而拖慢体验。）
RATE_LIMIT_INTERVAL = 1.0
RATE_LIMIT_RETRY_SECONDS = 6.0


class OpenAITranslator(DeepSeekTranslator):
    """OpenAI 文本翻译器。

    复用讯飞客户端的固定词条替换、稿件参考、翻译记忆等逻辑，
    只把网络请求换成 OpenAI chat completions（gpt-4o-mini 等）。
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = "gpt-4o-mini",
        *,
        glossary_entries: Optional[list[list[str]]] = None,
        reference_text: str = "",
    ) -> None:
        super().__init__(
            api_key,
            model,
            glossary_entries=glossary_entries,
            reference_text=reference_text,
        )

    def _system_prompt(self, text: str, source_lang: str, target_lang: str) -> str:
        # 父类用的是字面 "\\n"，DeepSeek 能容忍；gpt-4o-mini 会把字面 \n
        # 当作文本，导致词条表混进译文。这里替换成真实换行。
        return super()._system_prompt(text, source_lang, target_lang).replace(
            "\\n", "\n"
        )

    def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        text = text.strip()
        if not text:
            return ""
        if not self.api_key:
            raise RuntimeError("未配置 OpenAI API Key")

        corrected, corrections = self.correct_source_text(text)

        cached = lookup_manuscript_cache(corrected, target_lang)
        if cached is not None:
            if self._is_polluted(corrected) or self._is_polluted(cached):
                return ""
            return self._apply_glossary(corrected, cached, corrections)

        body = {
            "model": self.model,
            "temperature": 0.05,
            "max_tokens": 1000,
            "messages": [
                {
                    "role": "system",
                    "content": self._system_prompt(corrected, source_lang, target_lang),
                },
                {"role": "user", "content": corrected},
            ],
        }
        request = urllib.request.Request(
            CHAT_URL,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI 翻译 HTTP {exc.code}: {details[:300]}") from exc
        except Exception as exc:
            raise RuntimeError(f"OpenAI 翻译连接失败：{exc}") from exc

        try:
            translated = str(payload["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("OpenAI 翻译返回格式异常") from exc

        translated = self._apply_glossary(corrected, translated, corrections)
        if self._is_polluted(corrected) or self._is_polluted(translated):
            # 识别/翻译结果带提示词残留（模型回显等），不采用、不写缓存。
            return ""
        if translated:
            store_manuscript_cache(corrected, target_lang, translated)
        return translated

    @staticmethod
    def _is_polluted(text: str) -> bool:
        """检测文本是否混入了提示词/词条表内容（模型回显残留）。"""
        lower = str(text or "").casefold()
        markers = (
            "proper nouns",
            "fixed terms",
            "transcribe brand names",
            "=>",
            "context:",
        )
        return any(marker in lower for marker in markers)


class OpenAIInterpreter:
    """麦克风 -> OpenAI gpt-4o-transcribe 识别 -> gpt-4o-mini 翻译。

    国外模式：全程走 OpenAI 服务（冰岛/海外延迟低），不依赖国内接口。
    事件接口与 XfyunInterpreter / AzureInterpreter 保持一致。
    """

    def __init__(
        self,
        openai_api_key: str,
        on_event: Callable[[dict], None],
        *,
        direction: str = "zh_en",
        openai_transcribe_model: str = "gpt-4o-transcribe",
        openai_translate_model: str = "gpt-4o-mini",
        glossary_entries: Optional[list[list[str]]] = None,
        reference_text: str = "",
        translation_interval_mode: str = "adaptive",
        play_tts: bool = False,
        input_device: Optional[int] = None,
        session_id: int = 0,
        initial_segments: Optional[list[dict]] = None,
    ) -> None:
        self.openai_api_key = str(openai_api_key or "").strip()
        self.on_event = on_event
        self.direction = direction if direction in {"auto", "zh_en", "en_zh"} else "zh_en"
        self.transcribe_model = str(
            openai_transcribe_model or "gpt-4o-transcribe"
        ).strip()
        self.translator = OpenAITranslator(
            self.openai_api_key,
            str(openai_translate_model or "gpt-4o-mini").strip(),
            glossary_entries=glossary_entries,
            reference_text=reference_text,
        )
        self.play_tts = bool(play_tts)
        self.input_device = input_device
        self.session_id = int(session_id)
        self._initial_segments = [dict(segment) for segment in (initial_segments or [])]

        self._running = threading.Event()
        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=240)
        self._mic_stream: Any = None

        self._audio_buffer = b""
        self._last_transcribe_at = 0.0

        self._ordered_segments: list[dict[str, Any]] = []
        self._ordered_lock = threading.RLock()
        self._segment_seq = 0
        self._final_cn: list[str] = []
        self._final_en: list[str] = []
        self._current_source_language = "en" if self.direction == "en_zh" else "zh"

        self._translation_queue: queue.Queue[tuple[int, str]] = queue.Queue()
        self._translation_thread: Optional[threading.Thread] = None
        self._recognition_thread: Optional[threading.Thread] = None

    # ---------- 公共接口（与 XfyunInterpreter / AzureInterpreter 对齐） ----------

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
        if not self.openai_api_key:
            raise ValueError("OpenAI API Key 不能为空")

        self._reset_state()
        self._running.set()
        self._start_microphone()
        self._translation_thread = threading.Thread(
            target=self._translation_loop, daemon=True
        )
        self._translation_thread.start()
        self._recognition_thread = threading.Thread(
            target=self._recognition_loop, daemon=True
        )
        self._recognition_thread.start()
        self._emit("status", text="正在连接 OpenAI 语音服务…", state="connecting")
        self._emit("status", text="已连接 OpenAI，正在监听麦克风", state="connected")

    def stop(self) -> None:
        if not self._running.is_set():
            return
        self._running.clear()
        self._close_microphone()
        self._emit("closed", text="OpenAI 连接已结束")

    def set_play_tts(self, enabled: bool) -> None:
        self.play_tts = bool(enabled)

    def set_translation_interval_mode(self, mode: str) -> None:
        # OpenAI 模式按句翻译，无需间隔重译；保留接口以兼容 UI。
        pass

    def update_translation_materials(self, glossary_entries, reference_text: str) -> None:
        self.translator.set_context(glossary_entries or [], reference_text)

    def finalize_session(self, timeout: float = 20.0) -> dict[str, Any]:
        self.stop()
        deadline = time.monotonic() + max(5.0, timeout)
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))

        # 停止时若还有未落段的缓冲，补成正式段落并触发翻译。
        if self._audio_buffer:
            self._commit_segment(force=True)

        # 给实时阶段的最终翻译最多 8 秒完成。
        live_deadline = min(deadline, time.monotonic() + 8.0)
        while time.monotonic() < live_deadline:
            snapshot = self._segment_snapshot()
            pending = [
                s
                for s in snapshot
                if s.get("status") in {"pending", "translating"}
            ]
            completed = sum(1 for s in snapshot if str(s.get("translation", "")).strip())
            self._emit(
                "finalize_progress",
                completed=completed,
                total=len(snapshot),
                text=f"正在等待未完成译文：{completed}/{len(snapshot)}",
            )
            if not pending:
                break
            time.sleep(0.35)

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

    def _reset_state(self) -> None:
        self._ordered_segments = [dict(segment) for segment in self._initial_segments]
        self._segment_seq = len(self._ordered_segments)
        self._audio_buffer = b""
        self._last_transcribe_at = 0.0
        self._final_cn = []
        self._final_en = []
        self._current_source_language = "en" if self.direction == "en_zh" else "zh"

    def _emit(self, event_type: str, **data: object) -> None:
        self.on_event({"type": event_type, "session_id": self.session_id, **data})

    def _log(self, text: str) -> None:
        """写入软件日志（与 app.py 同一日志文件），便于排查识别/词条情况。"""
        try:
            import os

            if platform.system() == "Darwin":
                log_dir = (
                    Path.home()
                    / "Library"
                    / "Application Support"
                    / "ismolar-interpreter"
                    / "logs"
                )
            elif platform.system() == "Windows":
                log_dir = (
                    Path(os.environ.get("APPDATA", str(Path.home())))
                    / "ismolar-interpreter"
                    / "logs"
                )
            else:
                log_dir = Path.home() / ".config" / "ismolar-interpreter" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "app.log").open("a", encoding="utf-8") as fp:
                fp.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
        except Exception:
            pass

    def _start_microphone(self) -> None:
        def callback(indata: bytes, _frames: int, _time_info: object, status: object) -> None:
            if status:
                self._emit("status", text=f"音频提示：{status}", state="warning")
            if not self._running.is_set():
                return
            try:
                self._audio_queue.put_nowait(bytes(indata))
            except queue.Full:
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

    def _recognition_loop(self) -> None:
        while self._running.is_set():
            try:
                chunk = self._audio_queue.get(timeout=0.2)
            except queue.Empty:
                chunk = b""
            if chunk:
                self._audio_buffer += chunk
            self._maybe_process()

    def _buffer_seconds(self) -> float:
        return len(self._audio_buffer) / (SAMPLE_RATE * 2)

    def _tail_silent(self) -> bool:
        """检查缓冲尾部是否静音（用于断句）。"""
        n = int(SILENCE_END_SECONDS * SAMPLE_RATE) * 2
        tail = self._audio_buffer[-n:]
        if len(tail) < n:
            return False
        samples = struct.unpack(f"<{len(tail) // 2}h", tail)
        if not samples:
            return False
        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
        return rms < SILENCE_RMS_THRESHOLD

    def _maybe_process(self) -> None:
        if not self._running.is_set():
            return
        duration = self._buffer_seconds()
        if duration < SEGMENT_MIN_SECONDS:
            return
        if not self._buffer_has_speech():
            # 静音/纯噪声缓冲不送识别，防止模型把提示词回显成“识别结果”。
            self._audio_buffer = b""
            return
        # ponytail: 攒够一段才识别并落段，字幕按句更新（约 3~7 秒一行），
        # 不做逐字临时字幕。要更实时的上屏体验时，在这里加 interim 分支：
        # 静音前先转录显示半成品，落段时复用同一段结果。
        if duration >= SEGMENT_MAX_SECONDS or self._tail_silent():
            self._commit_segment()

    def _buffer_has_speech(self) -> bool:
        """粗略峰值检测：缓冲里只要出现过说话声浪就认为有语音。
        # ponytail: 阈值按麦克风环境可调；需要更稳的 VAD 时再换能量检测。"""
        n = len(self._audio_buffer)
        if n < 64:
            return False
        step = SAMPLE_RATE // 100  # 每 10ms 取一个样本
        samples = struct.unpack(f"<{n // 2}h", self._audio_buffer)
        return max(abs(s) for s in samples[::step]) > SPEECH_PEAK_THRESHOLD

    def _transcribe(self) -> tuple[str, list[tuple[str, str, str]]]:
        """把当前缓冲送去识别，返回 (修正后文本, 纠错列表)。
        每段音频只转录一次：攒够一段（尾部静音或到上限）才送识别。"""

        self._throttle_transcribe()
        wav_bytes = self._pcm_to_wav(self._audio_buffer)
        prompt = self._transcribe_prompt()
        language = "zh" if self.direction == "zh_en" else ""
        text, corrections = self._transcribe_request(wav_bytes, prompt, language)

        return text, corrections

    def _throttle_transcribe(self) -> None:
        """只防连发（间隔不低于 1 秒）；撞 429 由 _transcribe_request 重试。"""
        elapsed = time.monotonic() - self._last_transcribe_at
        if self._last_transcribe_at and elapsed < RATE_LIMIT_INTERVAL:
            time.sleep(RATE_LIMIT_INTERVAL - elapsed)
        self._last_transcribe_at = time.monotonic()

    def _transcribe_request(
        self, wav_bytes: bytes, prompt: str, language: str
    ) -> tuple[str, list[tuple[str, str, str]]]:
        boundary = uuid.uuid4().hex
        parts = []

        def field(name: str, value: str) -> None:
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")
            )

        field("model", self.transcribe_model)
        field("response_format", "json")
        if prompt:
            field("prompt", prompt)
        if language:
            field("language", language)
        parts.append(
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="file"; '
                'filename="audio.wav"\r\n'
                "Content-Type: audio/wav\r\n\r\n"
            ).encode("utf-8")
        )
        parts.append(wav_bytes)
        parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)

        request = urllib.request.Request(
            TRANSCRIBE_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {self.openai_api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        payload = None
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                last_error = RuntimeError(
                    f"OpenAI 识别 HTTP {exc.code}: {details[:300]}"
                )
                if exc.code == 429 and attempt < 2:
                    # ponytail: 限流时等 6 秒重试，最多 3 次；仍失败则交给上层。
                    time.sleep(RATE_LIMIT_RETRY_SECONDS)
                    continue
                raise last_error from exc
            except Exception as exc:
                # 网络抖动/SSL 中断：等 2 秒重试，最多 3 次。
                last_error = RuntimeError(f"OpenAI 识别连接失败：{exc}")
                if attempt < 2:
                    time.sleep(2.0)
                    continue
                raise last_error from exc
        if payload is None:
            raise last_error or RuntimeError("OpenAI 识别无响应")

        text = str(payload.get("text", "") or "").strip()
        if self._is_prompt_echo(text):
            return "", []
        if not text:
            return "", []
        corrected, corrections = self.translator.correct_source_text(text)
        if corrections:
            for span, canonical, replacement in corrections:
                self._log(
                    f"模糊纠错命中：{span.strip()[:40]!r} → "
                    f"{canonical.strip()[:40]!r}（替换译文：{replacement.strip()[:40]!r}）"
                )
        self._log(f"识别结果：{self._guess_lang(text)} {text.strip()[:60]!r} → {corrected.strip()[:60]!r}")
        return corrected, corrections

    @staticmethod
    def _pcm_to_wav(pcm: bytes) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm)
        return buf.getvalue()

    def _transcribe_prompt(self) -> str:
        entries = self.translator.glossary_entries
        if not entries:
            return ""
        names: list[str] = []
        for source, target in entries[:80]:
            for term in (source, target):
                term = str(term).strip()
                if term and term not in names:
                    names.append(term)
        # 纯词表提示：不带“指令句”，降低模型把提示当内容回显的概率。
        return "Proper nouns: " + ", ".join(names)

    def _is_prompt_echo(self, text: str) -> bool:
        """识别结果若与提示词高度相似（模型回显提示词），丢弃。"""
        norm_text = DeepSeekTranslator._normalize_term(text)
        if not norm_text:
            return True
        matched_terms: list[str] = []
        hits = 0
        for source, target in self.translator.glossary_entries:
            norm_source = DeepSeekTranslator._normalize_term(source)
            norm_target = DeepSeekTranslator._normalize_term(target)
            if norm_source and norm_source in norm_text:
                hits += 1
                matched_terms.append(norm_source)
            if norm_target and norm_target in norm_text:
                hits += 1
                matched_terms.append(norm_target)
        # 部分回显：极短文本里同时出现 ≥2 个词条项（如 “BIOEFFECT蓓欧菲”）。
        if len(norm_text) <= 16:
            if hits >= 2:
                return True
        # ponytail: 一句话里混入 ≥3 个词条项基本是噪声+提示词生成的幻觉
        # （真实口语很少连说三个词条），整体丢弃，避免脏字幕。
        if hits >= 3:
            return True
        # 词条字符占比过高：模型把词表拼成“品牌宣传句”（如
        # “BIOEFFECT蓓欧菲大麦生长因子护肤品牌”），即使只命中 2 个词条也丢弃。
        if hits >= 2:
            covered = [False] * len(norm_text)
            for term in matched_terms:
                if not term:
                    continue
                idx = norm_text.find(term)
                while idx != -1:
                    for i in range(idx, idx + len(term)):
                        covered[i] = True
                    idx = norm_text.find(term, idx + 1)
            if sum(covered) / len(norm_text) >= 0.7:
                return True
        prompt = self._transcribe_prompt()
        if not prompt:
            return False
        norm_prompt = DeepSeekTranslator._normalize_term(prompt)
        return bool(norm_prompt) and norm_text.startswith(norm_prompt[:12])

    def _guess_lang(self, text: str) -> str:
        if self.direction == "zh_en":
            return "zh"
        if self.direction == "en_zh":
            return "en"
        return detect_source_language(text)

    def _commit_segment(self, force: bool = False) -> None:
        if not self._audio_buffer:
            return
        try:
            text, _corrections = self._transcribe()
        except Exception as exc:
            self._audio_buffer = b""
            self._emit("error", text=f"识别失败：{exc}")
            return
        self._audio_buffer = b""

        text = text.strip()
        if not text:
            return

        source_lang = self._guess_lang(text)
        self._current_source_language = source_lang

        # 落段后翻译线程随后补齐译文。
        ordered_index = self._append_ordered_segment(
            text,
            "",
            source_lang,
            status="pending",
        )
        if source_lang == "zh":
            self._final_cn.append(text)
        else:
            self._final_en.append(text)
        self._emit_subtitles()

        self._translation_queue.put((ordered_index, text))
        if not force:
            self._emit("status", text="已连接 OpenAI，正在监听麦克风", state="connected")

    def _translation_loop(self) -> None:
        while self._running.is_set():
            try:
                index, source_text = self._translation_queue.get(timeout=0.3)
            except queue.Empty:
                continue
            segment = self._segment_at(index)
            if segment is None:
                continue
            source_lang = str(segment.get("source_language", "zh"))
            target_lang = "en" if source_lang == "zh" else "zh"
            try:
                self._update_ordered_translation(
                    index, "", status="translating"
                )
                translated = self.translator.translate(source_text, source_lang, target_lang)
                if translated:
                    # 译文写入字幕显示流（overlay 的 chinese/english 字段）。
                    if target_lang == "zh":
                        self._final_cn.append(translated)
                    else:
                        self._final_en.append(translated)
                    self._log(
                        f"翻译结果（{self.transcribe_model} → {self.translator.model}）："
                        f"{source_text.strip()[:40]!r} → {translated.strip()[:60]!r}"
                    )
                self._update_ordered_translation(
                    index, translated, status="complete" if translated else "pending"
                )
                if self.play_tts and translated:
                    self._speak_system(translated, language=target_lang)
            except Exception as exc:
                self._update_ordered_translation(
                    index, "", status="pending", error=str(exc)
                )
                self._emit("deepseek_error", text=str(exc), english=source_text)
            self._emit_subtitles()

    # ---------- 字幕 / 段落管理（与 Azure / 讯飞保持一致的结构） ----------

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

    def _segment_at(self, index: int) -> Optional[dict[str, Any]]:
        with self._ordered_lock:
            if not (0 <= index < len(self._ordered_segments)):
                return None
            return dict(self._ordered_segments[index])

    def _update_ordered_translation(
        self,
        index: int,
        translation: str,
        *,
        status: str | None = None,
        error: str = "",
    ) -> None:
        with self._ordered_lock:
            if not (0 <= index < len(self._ordered_segments)):
                return
            segment = self._ordered_segments[index]
            translation = translation.strip()
            segment["translation"] = translation
            if status is not None:
                segment["status"] = status
            if error:
                segment["error"] = error

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
            segments = [dict(segment) for segment in self._ordered_segments]
        chinese = "\n".join(self._final_cn)
        english = "\n".join(self._final_en)
        self._emit(
            "subtitles",
            chinese=chinese,
            english=english,
            source_language=self._current_source_language,
            source_transcript="\n".join(source_parts),
            translation_transcript="\n".join(translation_parts),
            segments=segments,
        )

    # ---------- TTS（系统朗读，免费；macOS say / Windows PowerShell） ----------

    def _speak_system(self, text: str, language: str) -> None:
        def worker() -> None:
            system = platform.system()
            try:
                if system == "Darwin":
                    voice = "Tingting" if language == "zh" else "Samantha"
                    subprocess.run(["say", "-v", voice, text], check=False)
                elif system == "Windows":
                    escaped = text.replace("'", "''")
                    script = (
                        "Add-Type -AssemblyName System.Speech; "
                        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                        f"$s.Speak('{escaped}')"
                    )
                    subprocess.run(
                        ["powershell", "-NoProfile", "-Command", script],
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        check=False,
                    )
            except Exception as exc:
                self._emit("error", text=f"系统语音播放失败：{exc}")

        threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
    # 自检：识别 -> 落段 -> 翻译 -> finalize 文档结构（无网络，全 mock）。
    import math
    import struct as _struct

    glossary = [["BIOEFFECT", "蓓欧菲"]]
    events: list[dict] = []
    it = OpenAIInterpreter(
        "sk-test", events.append, direction="en_zh", glossary_entries=glossary
    )

    def fake_transcribe(_wav: bytes, _prompt: str, _lang: str):
        return "BIOEFFECT is a good brand.", [("Byo Effect", "BIOEFFECT", "蓓欧菲")]

    it._transcribe_request = fake_transcribe
    it.translator.translate = lambda text, src, tgt: "蓓欧菲是个不错的品牌。"

    samples = [
        int(8000 * math.sin(2 * math.pi * 220 * i / SAMPLE_RATE))
        for i in range(SAMPLE_RATE * 4)
    ]
    it._audio_buffer = _struct.pack("<%dh" % len(samples), *samples)
    it._commit_segment()

    segment = it._segment_snapshot()[0]
    assert segment["source"] == "BIOEFFECT is a good brand.", segment
    assert segment["status"] == "pending", segment

    it._running.set()
    it._translation_thread = threading.Thread(
        target=it._translation_loop, daemon=True
    )
    it._translation_thread.start()
    time.sleep(0.5)
    segment = it._segment_snapshot()[0]
    assert segment["translation"] == "蓓欧菲是个不错的品牌。", segment

    result = it.finalize_session(timeout=5)
    assert "蓓欧菲" in result["chinese"], result
    print("OpenAI 客户端自检通过 ✔")
