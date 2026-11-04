"""Speech-to-text using faster-whisper. Runs locally, no API key needed."""
from __future__ import annotations

import base64
import ctypes
import gc
import os
import sys
import tempfile
import threading
import time
from typing import TYPE_CHECKING

from app.lang import pick_language
from app.log import log_event

if TYPE_CHECKING:
    # Only a type checker ever executes this. faster_whisper is imported inside
    # _load_model instead, because importing it drags in ctranslate2 and torch
    # for about 220 MiB in every process that imports app.main — a cost a
    # backend that only ever answers text messages used to pay from boot.
    from faster_whisper import WhisperModel

# "from __future__ import annotations" at the top is what keeps that saving.
# A module-level annotation is evaluated at import time and stored in
# __annotations__, so naming WhisperModel on the next line would quietly
# import the library again and undo the whole thing — and so would the next
# annotation anyone adds to this module. Quoting each one by hand works too,
# but only until somebody forgets the quotes.
_models: dict[str, WhisperModel] = {}
# When each cached model was last finished with, and how many threads are
# inside one right now. Both are keyed by model size, like _models.
_last_used: dict[str, float] = {}
_in_use: dict[str, int] = {}
# /webhook runs the agent in a thread pool and the dashboard transcribes in
# another, so two transcriptions can overlap and the bookkeeping above has to
# be locked.
_lock = threading.Lock()


# Kenyan WhatsApp voice notes are mostly English or a mix with Kiswahili, and
# Whisper does noticeably better on English than on Swahili. STT_LANGUAGE
# overrides it ("sw" to bias the other way).
DEFAULT_LANGUAGE = os.getenv("STT_LANGUAGE", "en")

# One voice note loads 610 MiB of Whisper that used to stay resident until the
# container was restarted, on a host already 88% into swap. Dropping an idle
# model costs the next caller the few seconds of a reload; keeping it costs the
# whole machine for days, so the default leans towards giving the memory back.
# Fifteen minutes is long enough that a back-and-forth exchange of voice notes
# pays the load once.
DEFAULT_IDLE_MINUTES = 15.0


def idle_window_seconds() -> float:
    """How long a cached model may sit unused before the next call drops it.

    MODEL_IDLE_MINUTES=0 keeps the old behaviour of never evicting. Read on
    each call rather than at import: a getenv costs nothing beside a
    transcription, and it keeps the window visible to the tests.
    """
    raw = os.getenv("MODEL_IDLE_MINUTES", "")
    try:
        minutes = float(raw) if raw else DEFAULT_IDLE_MINUTES
    except ValueError:
        # A typo in an env var should cost the default, not every transcription.
        log_event("model_idle_minutes_invalid", level="warning", value=raw)
        minutes = DEFAULT_IDLE_MINUTES
    return max(minutes, 0.0) * 60


def _load_model(size: str) -> WhisperModel:
    """The import that costs the 220 MiB, deferred to the first voice note."""
    from faster_whisper import WhisperModel

    return WhisperModel(size, device="cpu", compute_type="int8")


def _return_free_pages() -> None:
    """Hand the freed weights back to the operating system, not just to Python.

    Dropping the last reference is not enough on its own. ctranslate2 holds the
    weights in buffers reachable from the model object, so anything left in a
    reference cycle would be the worst of both outcomes — the next call pays
    the reload and the memory never came back; hence the collection. And an
    allocator keeps freed arenas for reuse rather than returning them, which is
    the retention MALLOC_ARENA_MAX=2 in docker-compose.yaml is already about:
    measured on macOS, an evict-and-reload cycle gave back under half of a
    "small" model. malloc_trim is glibc's answer to that, so it is worth asking
    for on the Linux container even though it is best effort and nothing here
    depends on it.
    """
    gc.collect()
    if sys.platform != "linux":
        return
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass  # not glibc, or a build without it: the collection above still stands


def _evict_idle(now: float) -> None:
    """Drop models nothing has touched for the idle window.

    Called with _lock held, and only from _acquire_model. This process serves
    webhooks and deliberately grows no background thread or asyncio task for
    this: a timer that fired in the middle of a transcription would be a worse
    problem than the memory it freed. The weakness of checking on entry is
    worth stating plainly — a process that stops receiving voice notes
    altogether never runs this again, so the last model of a burst stays
    resident until something else arrives. The win is the common case: one
    photo or one voice note no longer holds its model for the rest of the week.

    A model another thread is inside is never a candidate, which is what the
    _in_use count is for.
    """
    window = idle_window_seconds()
    if not window:
        return
    stale = [size for size in _models
             if not _in_use.get(size) and now - _last_used.get(size, now) >= window]
    for size in stale:
        log_event("whisper_model_evicted", size=size,
                  idle_minutes=round((now - _last_used.get(size, now)) / 60, 1))
        del _models[size]
        _last_used.pop(size, None)
        _in_use.pop(size, None)
    if stale:
        _return_free_pages()


def _acquire_model(size: str | None = None) -> tuple[str, WhisperModel]:
    """One cached model per size, so WhatsApp voice notes can use 'small' while
    the dashboard uses a bigger one (DASHBOARD_WHISPER_SIZE=medium).

    The caller must pass the returned size to _release_model when it is done,
    or that model can never be evicted again.
    """
    size = size or os.getenv("WHISPER_MODEL_SIZE", "small")
    with _lock:
        now = time.monotonic()
        _evict_idle(now)
        if size not in _models:
            # Loaded with the lock held. A second thread wanting the same size
            # would otherwise load a second 610 MiB copy of it, and with
            # OMP_NUM_THREADS=2 on 1.5 cpus there is nothing to win by loading
            # two models at once anyway.
            _models[size] = _load_model(size)
        _in_use[size] = _in_use.get(size, 0) + 1
        _last_used[size] = now
        return size, _models[size]


def _release_model(size: str) -> None:
    with _lock:
        _in_use[size] = max(_in_use.get(size, 1) - 1, 0)
        # Idle time runs from the end of a use, not the start: a four-minute
        # voice note should not count towards its own model's idleness.
        _last_used[size] = time.monotonic()


def _run_transcription(model: WhisperModel, audio: bytes, language: str, vad: bool) -> str:
    # delete=False + close before use: Windows can't reopen a temp file
    # that is still open in this process. The suffix doesn't matter: ffmpeg
    # (via PyAV) detects the real format from the content, so webm/mp4/ogg all work.
    with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as f:
        f.write(audio)
        path = f.name

    try:
        # "auto": let Whisper listen first, then keep it to English or Kiswahili
        segments, info = model.transcribe(path, language=None if language == "auto" else language, vad_filter=vad)
        if language == "auto":
            chosen = pick_language(info)
            if chosen != info.language:
                segments, info = model.transcribe(path, language=chosen, vad_filter=vad)
        return " ".join(seg.text.strip() for seg in segments).strip()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def transcribe_bytes(audio: bytes, language: str = DEFAULT_LANGUAGE, model_size: str | None = None, vad: bool = False) -> str:
    size, model = _acquire_model(model_size)
    try:
        return _run_transcription(model, audio, language, vad)
    finally:
        _release_model(size)


def transcribe_base64_audio(media_base64: str) -> str:
    return transcribe_bytes(base64.b64decode(media_base64), language=DEFAULT_LANGUAGE)
