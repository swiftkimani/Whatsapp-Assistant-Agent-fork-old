"""Speech-to-text: where a voice note becomes text.

Two implementations behind one entry point, chosen by STT_PROVIDER:

  whisper  faster-whisper, on this machine, free, no key, the default. The
           audio never leaves the host. Excellent on English (0.01-0.02 word
           error rate), unusable on Kiswahili and Sheng at every model size --
           see docs/host-memory.md and scripts/measure_whisper.py.
  gemini   Google's gemini-3.5-transcribe, in app/stt_gemini.py. Detects the
           language itself and is documented to handle code-switching inside a
           sentence, which is the actual shape of this corpus. Needs a key, and
           on the free tier the audio is other people's voice notes sent to a
           service whose terms say humans may read them -- app/stt_gemini.py's
           docstring, .env.example and README.md all say so where it matters.

Anything that is not "gemini" with a key behind it is the local path, which is
what nothing-in-the-environment gets.
"""
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
# overrides it ("sw" to bias the other way). The remote provider reads this
# too, but only to spot a deliberate "sw": it detects the language itself, and
# app/stt_gemini.py's _language_codes says why a hint would hurt it.
DEFAULT_LANGUAGE = os.getenv("STT_LANGUAGE", "en")

WHISPER = "whisper"
GEMINI = "gemini"

# Which of the two the first voice note of a process found missing, so a
# misconfiguration is said once instead of on every message. app/tts.py logs
# its fallback per failure because a TTS failure is an event; this one is a
# setting, and a setting that is wrong is wrong forever.
_said_once: set[str] = set()


def provider() -> str:
    """whisper unless something says otherwise, so nothing changes for anyone
    who sets nothing. Same shape as tts.provider()."""
    return os.getenv("STT_PROVIDER", WHISPER).strip().lower()


def _gemini():
    """app/stt_gemini.py, imported on the first voice note that needs it.

    Deferred for the reason the TYPE_CHECKING block above gives: importing
    app.main must not drag in a transcription stack. The module itself only
    needs the standard library and app.log today -- it is a REST call, not an
    SDK -- but the import stays inside the function so that stops being true
    loudly rather than quietly if anyone swaps the call for a Google SDK.
    tests/test_lazy_models.py holds that property.
    """
    from app import stt_gemini

    return stt_gemini


def use_gemini() -> bool:
    """Whether this transcription should go to Google.

    Both halves have to be true: the provider asked for, and a key present.
    No key is not an error -- it is the local path, running exactly as it did
    before, which is what makes this change safe to deploy before the key
    exists. It is still worth one line in the log the first time, because
    STT_PROVIDER=gemini with no key looks switched on and is not.
    """
    if provider() != GEMINI:
        return False
    if _gemini().api_key():
        return True
    if "no_key" not in _said_once:
        _said_once.add("no_key")
        log_event("stt_provider_has_no_key", level="warning", provider=GEMINI,
                  falling_back_to=WHISPER)
    return False


# Everything below here is the local provider.
#
# One voice note loads a model that used to stay resident until the container
# was restarted, on a host already 88% into swap — 272 MiB of resident growth
# for "base" and 562 MiB for "small", measured by scripts/measure_whisper.py.
# Dropping an idle model costs the next caller a reload (0.9 s for "base" from
# the model cache); keeping it costs the whole machine for days, so the default
# leans towards giving the memory back. Fifteen minutes is long enough that a
# back-and-forth exchange of voice notes pays the load once. The deployed
# container asks for five instead, and docker-compose.yaml says why there.
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


def evict_if_idle() -> None:
    """Run the idle check from outside a transcription.

    _evict_idle's own docstring names its weakness: checking only on entry
    means a process that stops receiving voice notes never runs it again, so
    the last model of a burst stays resident indefinitely. The healthcheck
    already runs every thirty seconds and is the cheapest heartbeat in the
    system, so calling this from there turns the idle window from a best case
    into something close to a guarantee -- without the background timer
    _evict_idle deliberately refuses to grow, because a timer firing in the
    middle of a transcription would cost more than the memory it freed.

    Takes the lock, because _evict_idle requires it held. Returns at once when
    nothing is loaded, which is the usual answer.
    """
    if not _models:
        return
    with _lock:
        _evict_idle(time.monotonic())


def _acquire_model(size: str | None = None) -> tuple[str, WhisperModel]:
    """One cached model per size, so WhatsApp voice notes can use 'base' while
    the dashboard uses a bigger one (DASHBOARD_WHISPER_SIZE=small).

    That flexibility costs a second set of weights resident, which is why
    docker-compose.yaml leaves DASHBOARD_WHISPER_SIZE empty: app/dashboard.py
    turns an empty value into None, and a None lands on the line below.

    The caller must pass the returned size to _release_model when it is done,
    or that model can never be evicted again.
    """
    size = size or os.getenv("WHISPER_MODEL_SIZE", "base")
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
    """Audio bytes to text, through whichever provider is configured.

    `model_size` and `vad` are Whisper's and are ignored by the remote
    provider, which has neither a size to pick nor a voice-activity filter to
    switch on. They stay in the signature because app/dashboard.py passes them
    positionally, and because turning the provider on must not change what
    this function means to a caller.

    Raises on failure -- stt_gemini.TranscriptionUnusable for a provider that
    could not answer, or whatever faster-whisper raises locally. The dashboard
    already answers that with a 422; transcribe_base64_audio below turns it
    into a held message.
    """
    if use_gemini():
        return _gemini().transcribe(audio, language=language)
    size, model = _acquire_model(model_size)
    try:
        return _run_transcription(model, audio, language, vad)
    finally:
        _release_model(size)


def transcribe_base64_audio(media_base64: str) -> str:
    """The WhatsApp path: base64 in, text out, and an empty string for a voice
    note that could not be transcribed.

    The empty string is the fail-closed answer, not a shrug. app/llm.py's
    chat_completion refuses to call the model with nothing to reply to and
    returns needs_owner, which verify() in app/agent_graph.py turns into a
    hold -- so an unreadable voice note reaches the owner in the Held tab and
    never reaches the sender as a guessed reply. That is the same path a voice
    note Whisper heard silence in has always taken.

    Only the provider's own typed failure is caught. A real bug in here still
    raises, because swallowing it would mean every voice note quietly becoming
    a hold with nobody the wiser.

    One thing this cannot do from inside this file: the reason below is written
    for the owner, but the Held tab shows the `why` that came from the reply
    model, and the one line that would carry this through -- a hold_reason set
    in app/agent_graph.py's classify_and_resolve, the way image_problem is --
    belongs to that file. Until then the reason is in the log.
    """
    audio = base64.b64decode(media_base64)
    if not use_gemini():
        return transcribe_bytes(audio, language=DEFAULT_LANGUAGE)
    try:
        return transcribe_bytes(audio, language=DEFAULT_LANGUAGE)
    except _gemini().TranscriptionUnusable as err:
        # No fall back to local Whisper, and this is the decision the rest of
        # this file leans on, so it is argued here rather than asserted.
        #
        # Whisper's failure on this corpus is not a worse transcript, it is a
        # confident one: docs/host-memory.md has a Swahili clip coming back as
        # fluent, unrelated English. Nothing downstream can catch that. The
        # verifier judges whether a *reply* is grounded in the prompt, and a
        # reply grounded in an invented message passes -- so a fallback would
        # turn "held, with a reason" into "answered, about something nobody
        # said", which is the one outcome this whole design exists to prevent.
        #
        # It is also what the memory is: keeping Whisper warm as a fallback
        # means keeping ctranslate2 and the weights installed and resident,
        # which is most of what switching providers could give back on a host
        # already deep into swap. Holding is both the safer answer and the
        # cheaper one, so there is no trade to make here.
        #
        # STT_PROVIDER=whisper remains one field in Coolify for anyone who
        # would rather have a wrong transcript than a wait.
        log_event("stt_unusable", level="warning", provider=GEMINI,
                  reason=str(err), kb=len(audio) // 1024)
        return ""
