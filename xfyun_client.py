from __future__ import annotations

import base64
import concurrent.futures
import difflib
import hashlib
import hmac
import json
import os
import platform
import queue
import re
import sqlite3
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from email.utils import formatdate
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import certifi
import sounddevice as sd
import websocket

HOST = "ws-api.xf-yun.com"
PATH = "/v1/private/simult_interpretation"
SERVICE_ID = "simult_interpretation"
SAMPLE_RATE = 16000
BLOCK_FRAMES = 640  # 40ms at 16kHz, 16-bit mono = 1280 bytes

# 稿件/句子翻译记忆：{(规范化原文, 目标语言): 译文}
# 稿件预翻译和实时翻译共用，命中后直接返回，跳过 DeepSeek 等待。
# 两级记忆：内存热缓存满后，先把最旧一批落盘到 SQLite 再腾位置；
# 查找时先查内存、未命中再查磁盘并加载回热缓存，磁盘记忆跨会话保留。
_MANUSCRIPT_CACHE: dict[tuple[str, str], str] = {}
_MANUSCRIPT_CACHE_LOCK = threading.Lock()
_MANUSCRIPT_CACHE_MAX = 800          # 内存热缓存条数上限
_MANUSCRIPT_FLUSH_BATCH = 100        # 每次腾位置时先落盘的条数
_MANUSCRIPT_DB_PATH: str | None = None
_MANUSCRIPT_DB_CONN: sqlite3.Connection | None = None
_MANUSCRIPT_DB_OK = False


def set_manuscript_cache_db(path) -> None:
    """启用磁盘翻译记忆；传 None 则只用内存热缓存。"""
    global _MANUSCRIPT_DB_PATH, _MANUSCRIPT_DB_CONN, _MANUSCRIPT_DB_OK
    with _MANUSCRIPT_CACHE_LOCK:
        _MANUSCRIPT_DB_PATH = str(path) if path else None
        if _MANUSCRIPT_DB_CONN is not None:
            try:
                _MANUSCRIPT_DB_CONN.close()
            except Exception:
                pass
            _MANUSCRIPT_DB_CONN = None
        _MANUSCRIPT_DB_OK = False
        if not _MANUSCRIPT_DB_PATH:
            return
        try:
            # 首次运行目录可能不存在；Windows 下 %APPDATA% 同理。
            db_dir = os.path.dirname(_MANUSCRIPT_DB_PATH)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            # check_same_thread=False：连接始终在 _MANUSCRIPT_CACHE_LOCK 内使用。
            conn = sqlite3.connect(_MANUSCRIPT_DB_PATH, check_same_thread=False)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS manuscript_memory ("
                " normalized_key TEXT NOT NULL,"
                " target_lang TEXT NOT NULL,"
                " translation TEXT NOT NULL,"
                " PRIMARY KEY (normalized_key, target_lang))"
            )
            conn.commit()
            if platform.system() != "Windows":
                # macOS 收紧为仅本人可读写；Windows 用 %APPDATA% 的 ACL 天然私有，
                # os.chmod 只影响只读位、无实际意义，故跳过。
                try:
                    os.chmod(_MANUSCRIPT_DB_PATH, 0o600)
                except OSError:
                    pass
            _MANUSCRIPT_DB_CONN = conn
            _MANUSCRIPT_DB_OK = True
        except Exception:
            _MANUSCRIPT_DB_OK = False


def _db_flush(items) -> None:
    if not _MANUSCRIPT_DB_OK or not items:
        return
    try:
        _MANUSCRIPT_DB_CONN.executemany(
            "INSERT OR REPLACE INTO manuscript_memory"
            " (normalized_key, target_lang, translation) VALUES (?, ?, ?)",
            [(key, lang, value) for (key, lang), value in items],
        )
        _MANUSCRIPT_DB_CONN.commit()
    except Exception:
        pass


def _db_get(key: str, target_lang: str) -> str | None:
    if not _MANUSCRIPT_DB_OK:
        return None
    try:
        row = _MANUSCRIPT_DB_CONN.execute(
            "SELECT translation FROM manuscript_memory"
            " WHERE normalized_key = ? AND target_lang = ?",
            (key, target_lang),
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _evict_oldest(count: int) -> None:
    """把最旧的一批先落盘，再从热缓存移除，为条目腾位置。"""
    items = list(_MANUSCRIPT_CACHE.items())[:count]
    if not items:
        return
    _db_flush(items)
    for key in [entry[0] for entry in items]:
        _MANUSCRIPT_CACHE.pop(key, None)


def detect_source_language(text: str) -> str:
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk == 0 and latin > 0:
        return "en"
    if latin == 0 and cjk > 0:
        return "zh"
    return "zh" if cjk >= max(1, round(latin * 0.28)) else "en"


def _normalize_match(text: str) -> str:
    # 去掉空白与标点，保留中英文和数字，便于 ASR 与稿件句子对碰。
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE).casefold()


def lookup_manuscript_cache(text: str, target_lang: str) -> str | None:
    # 统一缓存键：zh-CN / en-US 与 zh / en 视为同一语言。
    target_lang = str(target_lang or "").split("-")[0]
    key = _normalize_match(text)
    if len(key) < 2:
        return None
    with _MANUSCRIPT_CACHE_LOCK:
        exact = _MANUSCRIPT_CACHE.get((key, target_lang))
        if exact is not None:
            return exact
        # 内存未命中时从磁盘记忆加载精确条目，并放回热缓存。
        disk = _db_get(key, target_lang)
        if disk is not None:
            if len(_MANUSCRIPT_CACHE) >= _MANUSCRIPT_CACHE_MAX:
                _evict_oldest(_MANUSCRIPT_FLUSH_BATCH)
            _MANUSCRIPT_CACHE[(key, target_lang)] = disk
            return disk
        if len(key) < 4:
            return None
        best: str | None = None
        best_ratio = 0.0
        key_len = len(key)
        for (cached_key, cached_lang), cached_value in _MANUSCRIPT_CACHE.items():
            if cached_lang != target_lang:
                continue
            cached_len = len(cached_key)
            if cached_len < 4:
                continue
            if cached_len / key_len > 2.2 or key_len / cached_len > 2.2:
                continue
            ratio = difflib.SequenceMatcher(None, key, cached_key).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = cached_value
        return best if best_ratio >= 0.86 else None


def store_manuscript_cache(text: str, target_lang: str, translation: str) -> None:
    target_lang = str(target_lang or "").split("-")[0]
    key = _normalize_match(text)
    if len(key) < 2 or not translation.strip():
        return
    with _MANUSCRIPT_CACHE_LOCK:
        # 热缓存满了：先把最旧一批落盘并移除，再写入新条目。
        if len(_MANUSCRIPT_CACHE) >= _MANUSCRIPT_CACHE_MAX:
            _evict_oldest(_MANUSCRIPT_FLUSH_BATCH)
        value = translation.strip()
        _MANUSCRIPT_CACHE[(key, target_lang)] = value
        if _MANUSCRIPT_DB_OK:
            _db_flush([((key, target_lang), value)])


class DeepSeekTranslator:
    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-v4-flash",
        glossary_entries: Optional[list[list[str]]] = None,
        reference_text: str = "",
    ) -> None:
        self.api_key = api_key.strip()
        self.model = model.strip() or "deepseek-v4-flash"
        self.glossary_entries: list[list[str]] = []
        self.reference_text = ""
        self.set_context(glossary_entries or [], reference_text)

    @property
    def has_context(self) -> bool:
        return bool(self.glossary_entries or self.reference_text.strip())

    def set_context(
        self,
        glossary_entries: Optional[list[list[str]]],
        reference_text: str,
    ) -> None:
        cleaned: list[list[str]] = []
        for item in glossary_entries or []:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            source, target = str(item[0]).strip(), str(item[1]).strip()
            if source and target:
                cleaned.append([source, target])
            if len(cleaned) >= 500:
                break
        self.glossary_entries = cleaned
        self.reference_text = str(reference_text or "")[:40000]

    @staticmethod
    def _tokens(text: str) -> set[str]:
        latin = {
            token.casefold()
            for token in re.findall(r"[A-Za-z][A-Za-z0-9_\\-]{2,}", text)
        }
        cjk_runs = re.findall(r"[\\u3400-\\u9fff]{2,}", text)
        cjk: set[str] = set()
        for run in cjk_runs:
            if len(run) <= 4:
                cjk.add(run)
            else:
                cjk.update(run[i:i + 3] for i in range(len(run) - 2))
        return latin | cjk

    def _reference_excerpt(self, text: str) -> str:
        reference = self.reference_text.strip()
        if not reference:
            return ""

        paragraphs = [
            paragraph.strip()
            for paragraph in re.split(r"\\n{2,}|(?<=[。！？.!?])\\s+", reference)
            if paragraph.strip()
        ]
        if not paragraphs:
            return reference[:6000]

        query_tokens = self._tokens(text)
        scored: list[tuple[int, int, str]] = []
        for index, paragraph in enumerate(paragraphs):
            paragraph_tokens = self._tokens(paragraph)
            score = len(query_tokens & paragraph_tokens)
            for source, target in self.glossary_entries:
                if source.casefold() in text.casefold() and (
                    source.casefold() in paragraph.casefold()
                    or target.casefold() in paragraph.casefold()
                ):
                    score += 4
            scored.append((score, -index, paragraph))

        scored.sort(reverse=True)
        selected: list[str] = []
        total = 0
        for score, _negative_index, paragraph in scored:
            if selected and score <= 0:
                break
            remaining = 6000 - total
            if remaining <= 0:
                break
            selected.append(paragraph[:remaining])
            total += len(selected[-1])
            if len(selected) >= 10:
                break

        if not selected:
            return reference[:6000]
        return "\\n".join(selected)

    def _system_prompt(self, text: str, source_lang: str, target_lang: str) -> str:
        if source_lang == "en" and target_lang == "zh":
            direction = "将英文准确、自然地翻译成简体中文"
        elif source_lang == "zh" and target_lang == "en":
            direction = "将简体中文准确、自然地翻译成英文"
        else:
            direction = "准确、自然地翻译到目标语言"

        sections = [
            f"你是专业实时口译器。{direction}。",
            "只输出译文，不解释，不添加引号、标题或注释。",
            "保留数字、单位、语气和逻辑关系。",
        ]

        if self.glossary_entries:
            glossary_lines = [
                f"{source} => {target}"
                for source, target in self.glossary_entries[:200]
            ]
            sections.append(
                "以下是固定译法。只要原文出现左侧词语，必须使用右侧译法，"
                "不得改用同义词、简称或其他音译：\\n" + "\\n".join(glossary_lines)
            )

        excerpt = self._reference_excerpt(text)
        if excerpt:
            sections.append(
                "以下是参考稿件的相关内容。仅用于理解上下文、人名、职位、"
                "专业表达和预期措辞，不要照抄与当前原文无关的句子：\\n" + excerpt
            )

        return "\\n\\n".join(sections)

    def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        text = text.strip()
        if not text:
            return ""
        if not self.api_key:
            raise RuntimeError("未配置 DeepSeek API Key")

        cached = lookup_manuscript_cache(text, target_lang)
        if cached is not None:
            return self._apply_glossary(text, cached)

        body = {
            "model": self.model,
            "thinking": {"type": "disabled"},
            "temperature": 0.05,
            "max_tokens": 1000,
            "messages": [
                {
                    "role": "system",
                    "content": self._system_prompt(text, source_lang, target_lang),
                },
                {"role": "user", "content": text},
            ],
        }
        request = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
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
            raise RuntimeError(f"DeepSeek HTTP {exc.code}: {details[:300]}") from exc
        except Exception as exc:
            raise RuntimeError(f"DeepSeek 连接失败：{exc}") from exc

        try:
            translated = str(payload["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("DeepSeek 返回格式异常") from exc

        translated = self._apply_glossary(text, translated)
        store_manuscript_cache(text, target_lang, translated)
        return translated

    def _apply_glossary(self, text: str, translated: str) -> str:
        # 双向固定译法：原文出现左侧词 → 译文强制用右侧词；
        # 原文出现右侧词 → 译文强制用左侧词。一份词条两个方向都生效。
        result = translated
        for source, target in self.glossary_entries:
            if self._term_in_text(source, text):
                result = self._replace_term(source, target, result)
            elif self._term_in_text(target, text):
                result = self._replace_term(target, source, result)
        return result

    @staticmethod
    def _normalize_term(text: str) -> str:
        # 去掉大小写、空格与标点，只留字母数字：
        # BIOEFFECT / bio effect / bio-effect / Bio  Effect 视为同一个词。
        return "".join(ch for ch in str(text).casefold() if ch.isalnum())

    @staticmethod
    def _term_in_text(term: str, text: str) -> bool:
        norm = DeepSeekTranslator._normalize_term(term)
        return bool(norm) and norm in DeepSeekTranslator._normalize_term(text)

    @staticmethod
    def _replace_term(term: str, replacement: str, translated: str) -> str:
        norm = DeepSeekTranslator._normalize_term(term)
        if not norm:
            return translated
        if not re.search(r"[A-Za-z0-9]", norm):
            # 中文词：直接按原样替换。
            return re.sub(re.escape(term), replacement, translated)
        # 拉丁词：替换译文里“归一化后等于词条”的片段，容忍空格/连字符/大小写差异。
        pattern = re.compile(r"[A-Za-z0-9]+(?:[\s\-_.'’]+[A-Za-z0-9]+)*")

        def _sub(match) -> str:
            if DeepSeekTranslator._normalize_term(match.group(0)) == norm:
                return replacement
            return match.group(0)

        return pattern.sub(_sub, translated)

    def _reference_sentences(self, max_sentences: int = 300) -> list[str]:
        parts = re.split(r"[。！？!?；;.\n]+", self.reference_text)
        sentences: list[str] = []
        for part in parts:
            sentence = part.strip()
            if len(sentence) < 2 or len(sentence) > 400:
                continue
            sentences.append(sentence)
            if len(sentences) >= max_sentences:
                break
        return sentences

    def prewarm_reference(
        self,
        *,
        on_progress=None,
        on_result=None,
        should_stop=None,
        workers: int = 2,
    ) -> None:
        """后台把稿件逐句预翻译进翻译记忆；实时识别命中后直接取用。"""
        sentences = self._reference_sentences()
        if not sentences or not self.api_key:
            return
        total = len(sentences)
        if on_progress is not None:
            on_progress(0, total)
        done = 0
        progress_lock = threading.Lock()

        def work(sentence: str) -> None:
            nonlocal done
            source_lang = detect_source_language(sentence)
            target_lang = "zh" if source_lang == "en" else "en"
            translated = ""
            try:
                translated = self.translate(sentence, source_lang, target_lang)
            except Exception:
                pass
            finally:
                if on_result is not None:
                    try:
                        on_result(sentence, translated)
                    except Exception:
                        pass
                with progress_lock:
                    done += 1
                    if on_progress is not None:
                        on_progress(done, total)

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        try:
            futures = [
                pool.submit(work, sentence)
                for sentence in sentences
                if not (should_stop is not None and should_stop())
            ]
            for future in futures:
                if should_stop is not None and should_stop():
                    break
                future.result()
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def translate_en_to_zh(self, text: str) -> str:
        return self.translate(text, "en", "zh")

    def translate_zh_to_en(self, text: str) -> str:
        return self.translate(text, "zh", "en")


class XfyunInterpreter:
    """Microphone -> iFlytek ASR / simultaneous interpretation client."""

    def __init__(
        self,
        app_id: str,
        api_key: str,
        api_secret: str,
        on_event: Callable[[dict], None],
        *,
        direction: str = "zh_en",
        deepseek_api_key: str = "",
        deepseek_model: str = "deepseek-v4-flash",
        glossary_entries: Optional[list[list[str]]] = None,
        reference_text: str = "",
        translation_interval_mode: str = "adaptive",
        play_tts: bool = False,
        input_device: Optional[int] = None,
        session_id: int = 0,
        initial_segments: Optional[list[dict]] = None,
    ) -> None:
        self.app_id = app_id.strip()
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.on_event = on_event
        self.direction = direction if direction in {"auto", "zh_en", "en_zh"} else "zh_en"
        self.deepseek = DeepSeekTranslator(
            deepseek_api_key,
            deepseek_model,
            glossary_entries=glossary_entries,
            reference_text=reference_text,
        )
        self.translation_interval_mode = self._normalize_timing_mode(translation_interval_mode)
        self.play_tts = bool(play_tts)
        self.input_device = input_device
        self.session_id = int(session_id)
        # 断线自动重连时，新实例从已有段落继续，会议内容不丢失。
        self._initial_segments = [dict(segment) for segment in (initial_segments or [])]

        self._ws: Optional[websocket.WebSocketApp] = None
        self._running = threading.Event()
        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=120)
        self._tts_queue: queue.Queue[bytes] = queue.Queue(maxsize=240)
        self._mic_stream: Any = None
        self._tts_stream: Any = None
        self._seq = 0

        self._asr_segments: dict[int, str] = {}
        self._final_cn: list[str] = []
        self._final_en: list[str] = []
        self._interim_cn = ""
        self._interim_en = ""

        # Ordered records are only for display order. They prevent auto mode
        # from switching the whole overlay when the latest source language changes.
        self._ordered_segments: list[dict[str, Any]] = []
        self._ordered_interim_source = ""
        self._ordered_interim_translation = ""
        self._ordered_lock = threading.RLock()
        self._segment_seq = 0

        # 最终翻译最多同时执行两条，避免请求过多导致遗漏。
        self._translation_lock = threading.BoundedSemaphore(2)

        # 讯飞可能把一句英文拆成多个 sub_end；先合并再写入最终文档。
        self._english_buffer_lock = threading.RLock()
        self._english_sentence_buffer = ""
        self._english_commit_timer: Optional[threading.Timer] = None
        self._english_commit_generation = 0
        self._last_translated_english = ""
        self._current_source_language = "en" if self.direction == "en_zh" else "zh"
        self._latest_english_text = ""
        self._latest_english_updated_at = 0.0
        self._english_segment_started_at = 0.0
        self._last_translation_request_at = 0.0
        self._last_translation_requested_text = ""
        self._translation_request_seq = 0
        self._latest_translation_seq = 0
        # 讯飞英译中快译草稿：句子提交前到达，先上屏，DeepSeek 精修后替换一次。
        self._fast_translation_pending: tuple[str, str] | None = None

        # 稳定字幕模式：临时译文只在后台缓存，不写入浮窗。
        # 区间结束时，只有与最终原文完全匹配的最新缓存才会一次性显示。
        self._hidden_preview_lock = threading.RLock()
        self._hidden_preview_source = ""
        self._hidden_preview_translation = ""
        self._hidden_preview_seq = 0

    def _store_hidden_preview(
        self,
        source: str,
        translation: str,
        request_seq: int,
    ) -> None:
        source = source.strip()
        translation = translation.strip()
        if not source or not translation:
            return
        with self._hidden_preview_lock:
            if request_seq >= self._hidden_preview_seq:
                self._hidden_preview_source = source
                self._hidden_preview_translation = translation
                self._hidden_preview_seq = request_seq

    def _take_matching_hidden_preview(self, source: str) -> str:
        normalized = source.strip()
        with self._hidden_preview_lock:
            if (
                normalized
                and self._hidden_preview_source == normalized
                and self._hidden_preview_translation
            ):
                translation = self._hidden_preview_translation
                self._hidden_preview_source = ""
                self._hidden_preview_translation = ""
                self._hidden_preview_seq = 0
                return translation
        return ""

    def _clear_hidden_preview(self) -> None:
        with self._hidden_preview_lock:
            self._hidden_preview_source = ""
            self._hidden_preview_translation = ""
            self._hidden_preview_seq = 0

    def _set_ordered_interim(
        self,
        *,
        source: str | None = None,
        translation: str | None = None,
    ) -> None:
        with self._ordered_lock:
            if source is not None:
                self._ordered_interim_source = source.strip()
            if translation is not None:
                self._ordered_interim_translation = translation.strip()

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
            self._ordered_interim_source = ""
            self._ordered_interim_translation = ""
            return len(self._ordered_segments) - 1

    def _update_ordered_translation(
        self,
        index: int,
        translation: str,
        *,
        status: str = "complete",
        error: str = "",
    ) -> None:
        with self._ordered_lock:
            if 0 <= index < len(self._ordered_segments):
                self._ordered_segments[index]["translation"] = translation.strip()
                self._ordered_segments[index]["status"] = status
                self._ordered_segments[index]["error"] = error

    def _mark_segment_failed(self, index: int, error: str) -> None:
        with self._ordered_lock:
            if 0 <= index < len(self._ordered_segments):
                self._ordered_segments[index]["status"] = "failed"
                self._ordered_segments[index]["error"] = error

    def _segment_snapshot(self) -> list[dict[str, Any]]:
        with self._ordered_lock:
            return [dict(segment) for segment in self._ordered_segments]

    @staticmethod
    def _join_english(left: str, right: str) -> str:
        left, right = left.strip(), right.strip()
        if not left:
            return right
        if not right:
            return left
        if right[0] in ",.;:!?)]}%":
            return left + right
        return left + " " + right

    @staticmethod
    def _should_commit_english(text: str) -> bool:
        value = text.strip()
        if not value:
            return False
        if re.search(r'[.!?。！？]["\')\]]*$', value):
            return True
        words = len(re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)?", value))
        return words >= 28 or len(value) >= 180

    def _cancel_english_commit_timer(self) -> None:
        with self._english_buffer_lock:
            self._english_commit_generation += 1
            if self._english_commit_timer is not None:
                self._english_commit_timer.cancel()
                self._english_commit_timer = None

    def _schedule_english_commit(self, delay: float = 1.05) -> None:
        with self._english_buffer_lock:
            self._english_commit_generation += 1
            generation = self._english_commit_generation
            if self._english_commit_timer is not None:
                self._english_commit_timer.cancel()
            timer = threading.Timer(
                delay,
                lambda: self._commit_english_buffer(expected_generation=generation),
            )
            timer.daemon = True
            self._english_commit_timer = timer
            timer.start()

    def _commit_english_buffer(
        self,
        *,
        expected_generation: int | None = None,
        force: bool = False,
    ) -> None:
        with self._english_buffer_lock:
            if expected_generation is not None and expected_generation != self._english_commit_generation:
                return
            text = self._english_sentence_buffer.strip()
            if not text and force:
                text = self._latest_english_text.strip()
            if not text:
                return
            self._english_sentence_buffer = ""
            self._english_commit_generation += 1
            if self._english_commit_timer is not None:
                self._english_commit_timer.cancel()
                self._english_commit_timer = None

        if text == self._last_translated_english:
            return
        self._last_translated_english = text
        self._translation_request_seq += 1
        final_seq = self._translation_request_seq
        self._latest_translation_seq = final_seq
        self._final_en.append(text)
        ordered_index = self._append_ordered_segment(text, "", "en", status="pending")
        self._interim_en = ""
        self._latest_english_text = ""
        self._last_translation_requested_text = ""
        self._english_segment_started_at = 0.0
        self._set_ordered_interim(source="", translation="")

        # 讯飞英译中快译草稿：有则在 DeepSeek 精修期间先上屏。
        fast_draft = self._take_pending_fast_translation(text)
        if fast_draft:
            self._interim_cn = fast_draft
            self._set_ordered_interim(translation=fast_draft)

        # 区间内的后台预览只有在原文完全一致时才可直接锁定。
        # 这样既减少等待，也不会把不完整译文显示后再覆盖。
        cached_translation = self._take_matching_hidden_preview(text)
        if cached_translation:
            self._interim_cn = ""
            self._set_ordered_interim(translation="")
            self._final_cn.append(cached_translation)
            self._update_ordered_translation(
                ordered_index,
                cached_translation,
                status="complete",
            )
            self._emit_subtitles()
            if self.play_tts:
                self._speak_system(cached_translation, language="zh")
            self._emit(
                "status",
                text="本段译文已锁定，正在监听下一段",
                state="connected",
            )
            return

        self._emit_subtitles()
        self._translate_english(
            text,
            final=True,
            request_seq=final_seq,
            ordered_index=ordered_index,
            fallback_translation=fast_draft,
        )

    def _ordered_transcripts(self) -> tuple[str, str]:
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
            if self._ordered_interim_source.strip():
                source_parts.append(self._ordered_interim_source.strip())
            if self._ordered_interim_translation.strip():
                translation_parts.append(self._ordered_interim_translation.strip())
        return "\n".join(source_parts), "\n".join(translation_parts)

    @staticmethod
    def _normalize_timing_mode(mode: str) -> str:
        value = str(mode or "adaptive")
        return value if value in {"adaptive", "2", "3", "4", "5", "sentence"} else "adaptive"

    def set_translation_interval_mode(self, mode: str) -> None:
        self.translation_interval_mode = self._normalize_timing_mode(mode)

    def update_translation_materials(
        self,
        glossary_entries,
        reference_text: str,
    ) -> None:
        self.deepseek.set_context(glossary_entries or [], reference_text)
        if self._running.is_set() and self.deepseek.has_context:
            threading.Thread(target=self._prewarm_thread, daemon=True).start()

    def _prewarm_thread(self) -> None:
        self.deepseek.prewarm_reference(
            should_stop=lambda: not self._running.is_set()
        )

    def _effective_translation_interval(self, text: str) -> float | None:
        mode = self.translation_interval_mode
        if mode == "sentence":
            return None
        if mode in {"2", "3", "4", "5"}:
            return float(mode)
        length = len(text.strip())
        if length <= 35:
            return 2.0
        if length <= 80:
            return 3.0
        if length <= 140:
            return 4.0
        return 5.0

    @staticmethod
    def list_input_devices() -> list[tuple[int, str]]:
        devices: list[tuple[int, str]] = []
        for index, device in enumerate(sd.query_devices()):
            if int(device.get("max_input_channels", 0)) > 0:
                devices.append((index, str(device.get("name", f"Input {index}"))))
        return devices

    def _emit(self, event_type: str, **data: object) -> None:
        self.on_event({"type": event_type, "session_id": self.session_id, **data})

    def create_auth_url(self) -> str:
        date = formatdate(timeval=None, localtime=False, usegmt=True)
        signature_origin = f"host: {HOST}\ndate: {date}\nGET {PATH} HTTP/1.1"
        signature_sha = hmac.new(
            self.api_secret.encode("utf-8"),
            signature_origin.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        signature = base64.b64encode(signature_sha).decode("utf-8")
        authorization_origin = (
            f'api_key="{self.api_key}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{signature}"'
        )
        authorization = base64.b64encode(
            authorization_origin.encode("utf-8")
        ).decode("utf-8")
        query = urlencode(
            {
                "authorization": authorization,
                "date": date,
                "host": HOST,
                "serviceId": SERVICE_ID,
            }
        )
        return f"wss://{HOST}{PATH}?{query}"

    def _make_payload(self, status: int, audio: bytes) -> dict:
        language_type = 3 if self.direction == "en_zh" else 1
        stream_from, stream_to = (
            ("en", "cn") if self.direction == "en_zh" else ("cn", "en")
        )
        payload = {
            "header": {"app_id": self.app_id, "status": status},
            "parameter": {
                "ist": {
                    "accent": "mandarin",
                    "domain": "ist_ed_open",
                    "language": "zh_cn",
                    "language_type": language_type,
                    "vto": 15000,
                    "eos": 150000,
                },
                "streamtrans": {"from": stream_from, "to": stream_to},
                "tts": {
                    "vcn": "x2_catherine",
                    "tts_results": {
                        "encoding": "raw",
                        "sample_rate": SAMPLE_RATE,
                        "channels": 1,
                        "bit_depth": 16,
                        "frame_size": 0,
                    },
                },
            },
            "payload": {
                "data": {
                    "audio": base64.b64encode(audio).decode("utf-8"),
                    "encoding": "raw",
                    "sample_rate": SAMPLE_RATE,
                    "seq": self._seq,
                    "status": status,
                }
            },
        }
        self._seq += 1
        return payload

    def start(self) -> None:
        if self._running.is_set():
            return
        if not all((self.app_id, self.api_key, self.api_secret)):
            raise ValueError("讯飞 APPID、APIKey 和 APISecret 不能为空")
        if self.direction in {"auto", "en_zh"} and not self.deepseek.api_key:
            raise ValueError("自动识别和英译中模式需要 DeepSeek API Key")

        self._running.set()
        self._seq = 0
        self._asr_segments.clear()
        self._final_cn.clear()
        self._final_en.clear()
        self._interim_cn = ""
        self._interim_en = ""
        with self._ordered_lock:
            self._ordered_segments.clear()
            self._ordered_interim_source = ""
            self._ordered_interim_translation = ""
            self._segment_seq = 0
        if self._initial_segments:
            with self._ordered_lock:
                self._ordered_segments = [dict(segment) for segment in self._initial_segments]
                self._segment_seq = len(self._ordered_segments)
        with self._english_buffer_lock:
            self._english_sentence_buffer = ""
            self._english_commit_generation += 1
            if self._english_commit_timer is not None:
                self._english_commit_timer.cancel()
                self._english_commit_timer = None
            self._fast_translation_pending = None
        self._last_translated_english = ""
        self._current_source_language = "en" if self.direction == "en_zh" else "zh"
        self._latest_english_text = ""
        self._latest_english_updated_at = 0.0
        self._english_segment_started_at = 0.0
        self._last_translation_request_at = 0.0
        self._last_translation_requested_text = ""
        self._translation_request_seq = 0
        self._latest_translation_seq = 0
        with self._hidden_preview_lock:
            self._hidden_preview_source = ""
            self._hidden_preview_translation = ""
            self._hidden_preview_seq = 0

        self._ws = websocket.WebSocketApp(
            self.create_auth_url(),
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        if self.deepseek.has_context:
            threading.Thread(target=self._prewarm_thread, daemon=True).start()
        threading.Thread(target=self._run_websocket, daemon=True).start()
        self._emit("status", text="正在连接讯飞服务…", state="connecting")

    def stop(self) -> None:
        if not self._running.is_set():
            return
        self._running.clear()
        self._emit("status", text="正在停止…", state="stopping")
        self._close_microphone()

    def set_play_tts(self, enabled: bool) -> None:
        self.play_tts = bool(enabled)

    def _run_websocket(self) -> None:
        assert self._ws is not None
        self._ws.run_forever(
            sslopt={"cert_reqs": ssl.CERT_REQUIRED, "ca_certs": certifi.where()},
            ping_interval=20,
            ping_timeout=10,
        )

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        try:
            self._start_microphone()
            if self.direction == "zh_en" and self.play_tts:
                self._start_tts_player()
            threading.Thread(target=self._send_audio_loop, args=(ws,), daemon=True).start()
            if self.direction in {"auto", "en_zh"}:
                threading.Thread(target=self._translation_timer_loop, daemon=True).start()
            self._emit("status", text="已连接，正在监听麦克风", state="connected")
        except Exception as exc:
            self._emit("error", text=f"麦克风启动失败：{exc}")
            self._running.clear()
            ws.close()

    def _start_microphone(self) -> None:
        def callback(indata: bytes, _frames: int, _time_info: object, status: object) -> None:
            if status:
                self._emit("status", text=f"音频提示：{status}", state="warning")
            if not self._running.is_set():
                return
            try:
                self._audio_queue.put_nowait(bytes(indata))
            except queue.Full:
                try:
                    self._audio_queue.get_nowait()
                    self._audio_queue.put_nowait(bytes(indata))
                except queue.Empty:
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

    def _send_audio_loop(self, ws: websocket.WebSocketApp) -> None:
        first = True
        try:
            while self._running.is_set():
                try:
                    audio = self._audio_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                status = 0 if first else 1
                first = False
                ws.send(json.dumps(self._make_payload(status, audio), ensure_ascii=False))
            ws.send(json.dumps(self._make_payload(2, b""), ensure_ascii=False))
        except Exception as exc:
            if self._running.is_set():
                self._emit("error", text=f"发送音频失败：{exc}")
            try:
                ws.close()
            except Exception:
                pass

    def _on_message(self, ws: websocket.WebSocketApp, message: object) -> None:
        # WebSocket 服务偶尔会产生空帧，某些网络代理也可能插入非 JSON
        # 内容。单个异常帧不应终止整场传译。
        if message is None:
            return

        if isinstance(message, bytes):
            raw_message = message.decode("utf-8", errors="replace").strip()
        else:
            raw_message = str(message).strip()

        if not raw_message:
            self._emit(
                "protocol_warning",
                text="收到讯飞空响应，已自动忽略并继续监听。",
                detail="empty websocket frame",
            )
            return

        try:
            packet = json.loads(raw_message)
        except json.JSONDecodeError as exc:
            preview = raw_message[:240].replace("\n", " ")
            self._emit(
                "protocol_warning",
                text="收到非标准返回内容，已自动忽略并继续监听。",
                detail=f"top-level JSON error: {exc}; preview={preview!r}",
            )
            return

        if not isinstance(packet, dict):
            self._emit(
                "protocol_warning",
                text="讯飞返回格式异常，已自动忽略并继续监听。",
                detail=f"top-level type={type(packet).__name__}",
            )
            return

        try:
            header = packet.get("header", {}) or {}
            code = int(header.get("code", 0) or 0)
            if code != 0:
                self._emit(
                    "error",
                    code=code,
                    text=f"讯飞返回错误 {code}：{header.get('message', '未知错误')}",
                )
                self._running.clear()
                self._close_microphone()
                ws.close()
                return

            payload = packet.get("payload", {}) or {}
            if not isinstance(payload, dict):
                self._emit(
                    "protocol_warning",
                    text="讯飞返回的数据区格式异常，已跳过当前数据。",
                    detail=f"payload type={type(payload).__name__}",
                )
                return

            recognition = payload.get("recognition_results")
            if isinstance(recognition, dict):
                self._handle_asr(recognition)

            translation = payload.get("streamtrans_results")
            if isinstance(translation, dict) and self.direction == "en_zh":
                self._handle_english_translation(translation)
            if (
                isinstance(translation, dict)
                and (
                    self.direction == "zh_en"
                    or (
                        self.direction == "auto"
                        and self._current_source_language == "zh"
                    )
                )
            ):
                self._handle_translation(translation)

            tts = payload.get("tts_results")
            if self.direction == "zh_en" and isinstance(tts, dict):
                self._handle_tts(tts)

            if int(header.get("status", 1) or 1) == 2:
                self._running.clear()
                self._close_microphone()
                self._close_tts()
                self._emit("closed", text="传译已结束")
                ws.close()
        except ValueError as exc:
            # payload 内部的 Base64/JSON 片段偶尔为空或不完整。只跳过当前片段。
            self._emit(
                "protocol_warning",
                text="收到一个不完整的识别片段，已跳过并继续监听。",
                detail=str(exc),
            )
        except Exception as exc:
            self._emit(
                "protocol_warning",
                text="处理当前返回片段时发生异常，已跳过并继续监听。",
                detail=f"{type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _decode_json_text(encoded_text: object) -> dict:
        if encoded_text is None:
            return {}

        encoded = str(encoded_text).strip()
        if not encoded:
            return {}

        try:
            raw_bytes = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ValueError(f"Base64 数据无效：{exc}") from exc

        if not raw_bytes:
            return {}

        raw = raw_bytes.decode("utf-8", errors="replace").strip()
        if not raw:
            return {}

        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            preview = raw[:200].replace("\n", " ")
            raise ValueError(
                f"内部 JSON 无效：{exc}；内容预览：{preview!r}"
            ) from exc

        if not isinstance(decoded, dict):
            raise ValueError(
                f"内部数据应为对象，实际为 {type(decoded).__name__}"
            )
        return decoded

    def _handle_asr(self, result: dict) -> None:
        decoded = self._decode_json_text(result.get("text", ""))
        if not decoded:
            return
        words: list[str] = []
        for item in decoded.get("ws", []):
            candidates = item.get("cw", [])
            if candidates:
                words.append(str(candidates[0].get("w", "")))
        text = "".join(words)
        sn = int(decoded.get("sn", len(self._asr_segments) + 1))
        pgs = decoded.get("pgs", "apd")
        if pgs == "rpl":
            rg = decoded.get("rg", [])
            if isinstance(rg, list) and len(rg) == 2:
                start, end = int(rg[0]), int(rg[1])
                for key in list(self._asr_segments):
                    if start <= key <= end:
                        self._asr_segments.pop(key, None)
        self._asr_segments[sn] = text
        full_text = "".join(self._asr_segments[key] for key in sorted(self._asr_segments))
        sentence = full_text.strip()

        if self.direction == "auto":
            source_language = detect_source_language(sentence)
        elif self.direction == "en_zh":
            source_language = "en"
        else:
            source_language = "zh"
        self._current_source_language = source_language

        recognition_status = int(result.get("status", 1))
        sentence_finished = (
            bool(decoded.get("sub_end"))
            or bool(decoded.get("ls"))
            or recognition_status == 2
        )

        if source_language == "zh":
            self._interim_cn = full_text
            self._set_ordered_interim(source=full_text)
            self._emit_subtitles()
            if sentence_finished:
                self._asr_segments.clear()
        else:
            self._cancel_english_commit_timer()
            with self._english_buffer_lock:
                prefix = self._english_sentence_buffer
            combined = self._join_english(prefix, sentence)
            self._interim_en = combined
            self._set_ordered_interim(source=combined)
            now = time.monotonic()
            if not self._latest_english_text:
                self._english_segment_started_at = now
            self._latest_english_text = combined
            self._latest_english_updated_at = now
            self._emit_subtitles()
            if sentence_finished and sentence:
                with self._english_buffer_lock:
                    self._english_sentence_buffer = combined
                self._asr_segments.clear()
                if self._should_commit_english(combined):
                    self._commit_english_buffer(force=True)
                else:
                    self._schedule_english_commit()

    def _handle_translation(self, result: dict) -> None:
        self._current_source_language = "zh"
        decoded = self._decode_json_text(result.get("text", ""))
        if not decoded:
            return
        src = str(decoded.get("src", "")).strip()
        dst = str(decoded.get("dst", "")).strip()
        is_final = int(decoded.get("is_final", 0)) == 1

        if is_final:
            if src:
                self._final_cn.append(src)
            self._interim_cn = ""

            ordered_index = self._append_ordered_segment(
                src,
                dst,
                "zh",
                status="refining" if (
                    src and self.deepseek.api_key and self.deepseek.has_context
                ) else ("complete" if dst else "pending"),
            )

            if src and self.deepseek.api_key and self.deepseek.has_context:
                # Display the fast iFlytek result first, then replace only this
                # sentence's translation. Earlier history remains untouched.
                self._interim_en = dst
                self._emit_subtitles()
                self._translate_chinese_with_context(src, ordered_index)
                return

            if dst:
                self._final_en.append(dst)
            self._interim_en = ""
        else:
            self._interim_cn = src
            self._interim_en = dst
            self._set_ordered_interim(source=src, translation=dst)

        self._emit_subtitles()

    def _handle_english_translation(self, result: dict) -> None:
        """英译中模式：讯飞 en→cn 快译草稿，先上屏，DeepSeek 精修后替换一次。"""
        decoded = self._decode_json_text(result.get("text", ""))
        if not decoded:
            return
        src = str(decoded.get("src", "")).strip()
        dst = str(decoded.get("dst", "")).strip()
        is_final = int(decoded.get("is_final", 0)) == 1
        if not is_final or not src or not dst:
            return

        with self._english_buffer_lock:
            latest = self._latest_english_text.strip()
        if not latest or not self._english_texts_match(latest, src):
            return

        committed = False
        with self._ordered_lock:
            for segment in self._ordered_segments:
                if (
                    str(segment.get("source_language", "")) == "en"
                    and self._english_texts_match(str(segment.get("source", "")), src)
                ):
                    committed = True
                    break

        with self._english_buffer_lock:
            self._fast_translation_pending = (src, dst)

        if not committed:
            # 句子尚未正式提交：直接显示快译草稿，等 DeepSeek 精修后替换。
            self._interim_cn = dst
            self._set_ordered_interim(translation=dst)
            self._emit_subtitles()

    @staticmethod
    def _english_texts_match(left: str, right: str) -> bool:
        key_left = _normalize_match(left)
        key_right = _normalize_match(right)
        if not key_left or not key_right:
            return False
        if len(key_left) < 4 or len(key_right) < 4:
            return key_left == key_right
        return (
            key_left == key_right
            or key_left.startswith(key_right)
            or key_right.startswith(key_left)
        )

    def _take_pending_fast_translation(self, text: str) -> str:
        with self._english_buffer_lock:
            pending = self._fast_translation_pending
            self._fast_translation_pending = None
        if pending and self._english_texts_match(pending[0], text):
            return pending[1].strip()
        return ""

    def _translate_chinese_with_context(self, chinese: str, ordered_index: int) -> None:
        def worker() -> None:
            with self._translation_lock:
                try:
                    self._emit(
                        "status",
                        text="正在按固定译法和参考稿修正英文译文…",
                        state="translating",
                    )
                    english = self.deepseek.translate_zh_to_en(chinese)
                    if english:
                        self._final_en.append(english)
                        self._update_ordered_translation(
                            ordered_index,
                            english,
                            status="complete",
                        )
                    self._interim_en = ""
                    self._emit_subtitles()
                    if self.play_tts and english:
                        self._speak_system(english, language="en")
                    self._emit("status", text="已连接，正在监听麦克风", state="connected")
                except Exception as exc:
                    # Keep the iFlytek provisional translation visible on failure.
                    if self._interim_en:
                        self._final_en.append(self._interim_en)
                        self._update_ordered_translation(
                            ordered_index,
                            self._interim_en,
                            status="complete",
                            error=str(exc),
                        )
                    else:
                        self._mark_segment_failed(ordered_index, str(exc))
                    self._interim_en = ""
                    self._emit_subtitles()
                    self._emit("deepseek_error", text=str(exc), chinese=chinese)

        threading.Thread(target=worker, daemon=True).start()

    def _translation_timer_loop(self) -> None:
        while self._running.is_set():
            time.sleep(0.25)
            if self._current_source_language != "en":
                continue
            text = self._latest_english_text.strip()
            if not text or text == self._last_translation_requested_text:
                continue
            interval = self._effective_translation_interval(text)
            if interval is None:
                continue
            now = time.monotonic()
            anchor = self._last_translation_request_at or self._english_segment_started_at or now
            if now - anchor < interval:
                continue
            self._translation_request_seq += 1
            request_seq = self._translation_request_seq
            self._latest_translation_seq = request_seq
            self._last_translation_request_at = now
            self._last_translation_requested_text = text
            self._translate_english(text, final=False, request_seq=request_seq)

    def _translate_english(
        self,
        english: str,
        *,
        final: bool,
        request_seq: int,
        ordered_index: int | None = None,
        fallback_translation: str = "",
    ) -> None:
        def worker() -> None:
            acquired = self._translation_lock.acquire(blocking=final)
            if not acquired:
                if not final:
                    self._last_translation_requested_text = ""
                return
            try:
                self._emit(
                    "status",
                    text="DeepSeek 正在生成本段最终译文…" if final else "正在后台整理本段译文…",
                    state="translating",
                )
                chinese = self.deepseek.translate_en_to_zh(english)
                if not final and request_seq < self._latest_translation_seq:
                    return
                if final:
                    if chinese:
                        self._final_cn.append(chinese)
                        if ordered_index is not None:
                            self._update_ordered_translation(
                                ordered_index,
                                chinese,
                                status="complete",
                            )
                    elif fallback_translation:
                        # DeepSeek 返回空译文时保留讯飞快译，段落仍完整。
                        self._final_cn.append(fallback_translation)
                        if ordered_index is not None:
                            self._update_ordered_translation(
                                ordered_index,
                                fallback_translation,
                                status="complete",
                                error="DeepSeek 返回空译文，已使用讯飞快译",
                            )
                    elif ordered_index is not None:
                        self._mark_segment_failed(
                            ordered_index,
                            "DeepSeek 返回空译文",
                        )
                    self._interim_cn = ""
                    self._last_translation_requested_text = ""
                    self._set_ordered_interim(translation="")
                    self._clear_hidden_preview()
                else:
                    # 稳定字幕模式：区间内的译文只缓存，不显示、不重排、
                    # 不覆盖浮窗中的历史内容。区间结束后一次性锁定。
                    self._store_hidden_preview(
                        english,
                        chinese,
                        request_seq,
                    )
                    self._interim_cn = ""
                    self._set_ordered_interim(translation="")
                    self._emit(
                        "status",
                        text="正在后台整理本段译文…",
                        state="translating",
                    )
                if final:
                    self._emit_subtitles()
                if final and self.play_tts and chinese:
                    self._speak_system(chinese, language="zh")
                self._emit("status", text="已连接，正在监听麦克风", state="connected")
            except Exception as exc:
                if final and ordered_index is not None:
                    if fallback_translation:
                        self._final_cn.append(fallback_translation)
                        self._update_ordered_translation(
                            ordered_index,
                            fallback_translation,
                            status="complete",
                            error=str(exc),
                        )
                        self._interim_cn = ""
                        self._set_ordered_interim(translation="")
                    else:
                        self._mark_segment_failed(ordered_index, str(exc))
                    self._emit_subtitles()
                self._emit("deepseek_error", text=str(exc), english=english)
            finally:
                self._translation_lock.release()

        threading.Thread(target=worker, daemon=True).start()

    def _emit_subtitles(self) -> None:
        chinese = "\n".join(self._final_cn + ([self._interim_cn] if self._interim_cn else []))
        english = "\n".join(self._final_en + ([self._interim_en] if self._interim_en else []))
        source_transcript, translation_transcript = self._ordered_transcripts()
        self._emit(
            "subtitles",
            chinese=chinese,
            english=english,
            source_language=self._current_source_language,
            source_transcript=source_transcript,
            translation_transcript=translation_transcript,
            # 临时字幕只用于浮窗，不进入最终文档。
            segments=self._segment_snapshot(),
        )


    def _translate_missing_segment(self, index: int) -> tuple[int, str, str]:
        with self._ordered_lock:
            if not (0 <= index < len(self._ordered_segments)):
                return index, "", "段落不存在"
            segment = dict(self._ordered_segments[index])
        source = str(segment.get("source", "")).strip()
        language = str(segment.get("source_language", "zh"))
        if not source:
            return index, "", "原文为空"
        last_error = ""
        for attempt, delay in enumerate((0.0, 1.0, 2.0), start=1):
            if delay:
                time.sleep(delay)
            try:
                if language == "en":
                    translated = self.deepseek.translate_en_to_zh(source)
                else:
                    translated = self.deepseek.translate_zh_to_en(source)
                if translated.strip():
                    return index, translated.strip(), ""
                last_error = "DeepSeek 返回空译文"
            except Exception as exc:
                last_error = str(exc)
        return index, "", last_error or "补译失败"

    def finalize_session(self, timeout: float = 20.0) -> dict[str, Any]:
        self.stop()
        deadline = time.monotonic() + max(5.0, timeout)
        # 等待讯飞最后一个片段返回。
        time.sleep(min(1.2, max(0.0, deadline - time.monotonic())))

        # 停止发生在中文句子中间时，讯飞可能尚未来得及返回最终翻译。
        # 把当前中文原文保存为正式段落，后续由自动补译补齐英文。
        if self._current_source_language == "zh":
            pending_cn = self._interim_cn.strip()
            pending_en = self._interim_en.strip()
            if pending_cn:
                snapshot = self._segment_snapshot()
                last_source = str(snapshot[-1].get("source", "")).strip() if snapshot else ""
                if pending_cn != last_source:
                    self._final_cn.append(pending_cn)
                    self._append_ordered_segment(
                        pending_cn,
                        pending_en,
                        "zh",
                        status="complete" if pending_en else "pending",
                    )
                self._interim_cn = ""
                self._interim_en = ""
                self._set_ordered_interim(source="", translation="")

        self._cancel_english_commit_timer()
        self._commit_english_buffer(force=True)

        # 给实时阶段的最终翻译最多 8 秒完成。
        live_deadline = min(deadline, time.monotonic() + 8.0)
        while time.monotonic() < live_deadline:
            snapshot = self._segment_snapshot()
            pending = [
                s for s in snapshot
                if s.get("status") in {"pending", "translating", "refining"}
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

        missing = [
            i for i, segment in enumerate(self._segment_snapshot())
            if str(segment.get("source", "")).strip()
            and not str(segment.get("translation", "")).strip()
        ]

        if missing and self.deepseek.api_key and time.monotonic() < deadline:
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
            futures = {
                pool.submit(self._translate_missing_segment, index): index
                for index in missing
            }
            remaining = max(0.1, deadline - time.monotonic())
            done, not_done = concurrent.futures.wait(
                futures,
                timeout=remaining,
                return_when=concurrent.futures.ALL_COMPLETED,
            )
            for future in done:
                try:
                    index, translated, error = future.result()
                except Exception as exc:
                    index = futures[future]
                    translated, error = "", str(exc)
                if translated:
                    self._update_ordered_translation(index, translated, status="complete")
                else:
                    self._mark_segment_failed(index, error)
                snap = self._segment_snapshot()
                completed = sum(
                    1 for segment in snap
                    if str(segment.get("translation", "")).strip()
                )
                self._emit(
                    "finalize_progress",
                    completed=completed,
                    total=len(snap),
                    text=f"正在自动补译缺失内容：{completed}/{len(snap)}",
                )
                self._emit_subtitles()
            for future in not_done:
                future.cancel()
                self._mark_segment_failed(
                    futures[future],
                    "补译等待超时",
                )
            pool.shutdown(wait=False, cancel_futures=True)
        elif missing:
            for index in missing:
                self._mark_segment_failed(index, "未配置 DeepSeek，无法补译")

        segments = self._segment_snapshot()
        chinese_lines, english_lines = [], []
        for segment in segments:
            source = str(segment.get("source", "")).strip()
            translation = str(segment.get("translation", "")).strip()
            if str(segment.get("source_language", "zh")) == "en":
                if translation: chinese_lines.append(translation)
                if source: english_lines.append(source)
            else:
                if source: chinese_lines.append(source)
                if translation: english_lines.append(translation)
        completed = sum(1 for s in segments if str(s.get("translation", "")).strip())
        return {
            "segments": segments,
            "chinese": "\\n".join(chinese_lines),
            "english": "\\n".join(english_lines),
            "completed": completed,
            "total": len(segments),
        }

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

    def _handle_tts(self, result: dict) -> None:
        if not self.play_tts:
            return
        encoded_audio = result.get("audio")
        if not encoded_audio:
            return
        if self._tts_stream is None:
            self._start_tts_player()
        try:
            self._tts_queue.put_nowait(base64.b64decode(encoded_audio))
        except queue.Full:
            pass

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
                    self._emit("error", text=f"播放英文语音失败：{exc}")
                    break

    def _close_tts(self) -> None:
        if self._tts_stream is not None:
            try:
                self._tts_stream.stop()
                self._tts_stream.close()
            except Exception:
                pass
            self._tts_stream = None

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

    def _on_error(self, _ws: websocket.WebSocketApp, error: object) -> None:
        if self._running.is_set():
            self._emit("error", text=f"WebSocket 连接错误：{error}")
        self._running.clear()
        self._close_microphone()
        self._close_tts()

    def _on_close(
        self,
        _ws: websocket.WebSocketApp,
        close_status_code: Optional[int],
        close_msg: Optional[str],
    ) -> None:
        self._running.clear()
        self._close_microphone()
        self._close_tts()
        detail = f"（{close_status_code}: {close_msg}）" if close_status_code else ""
        self._emit("closed", text=f"连接已关闭{detail}")
