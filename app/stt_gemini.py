"""Transcription through Google's Gemini 3.5 Transcribe, for the languages
faster-whisper cannot read.

docs/host-memory.md has the measurement this exists for: across sixteen
WhatsApp-shaped clips, local Whisper scores 0.01-0.02 word error rate on Kenyan
English and *above 1.0* on Kiswahili and Sheng at every model size -- more
errors than the reference has words, and one clip came back as fluent English
that had nothing to do with the audio. Model size is not the lever. A model
that auto-detects the language and is documented to handle code-switching
inside one sentence is a different lever, and this file is it.

Verified against https://aistudio.google.com/docs/transcribe and
https://ai.google.dev/api/interactions-api (both read 2026-11-05):

    POST https://generativelanguage.googleapis.com/v1beta/interactions
    x-goog-api-key: <key>
    Content-Type: application/json

    {"model": "models/gemini-3.5-transcribe",
     "input": [{"type": "audio", "data": "<base64>", "mime_type": "audio/ogg"}],
     "generation_config": {"transcription_config": {"language_codes": []}}}

    200 {"status": "completed",
         "steps": [{"type": "model_output",
                    "content": [{"type": "text", "text": "..."}]}]}

`audio/ogg` is in the model's own list of accepted MIME types, which is what
WhatsApp sends a voice note as, so nothing is re-encoded here.

PRIVACY. These are other people's voice notes, and the owner has chosen the
free tier knowing the trade-off. Google's terms for unpaid use say in so many
words that "human reviewers may read, annotate, and process your API input and
output" and "Do not submit sensitive, confidential, or personal information to
the Unpaid Services" (https://aistudio.google.com/docs/terms). The pricing page
puts it as "Used to improve our products: Yes" for the free tier and "No" for
the paid one. That decision is the owner's; what this file owes him is that it
is stated where the switch is (.env.example), where the claim used to read the
other way (README.md's privacy table), and here. Two things follow in code:
the audio goes inline in the one request rather than through the Files API, so
nothing is left sitting in a Google file store afterwards, and nothing of what
was said is ever logged.
"""
# Same reason app/stt.py and app/vision.py keep this line: a module-level
# annotation is evaluated at import time, so naming a type from the deferred
# import below -- in an annotation here, or in the next one somebody adds --
# would quietly import it from boot and undo the saving.
from __future__ import annotations

import base64
import os
import random
import time
from typing import TYPE_CHECKING

from app.log import log_event

if TYPE_CHECKING:
    # Only a type checker ever executes this. httpx is imported inside _post
    # for the same reason faster_whisper is imported inside stt._load_model:
    # the property that app/stt.py's long comment protects is that importing
    # app.main pulls in nothing a text message does not need. httpx is already
    # in the process by way of the Anthropic SDK, so the saving here is small
    # and the discipline is the point -- it is also what keeps a Google SDK
    # out of the boot path if anyone ever swaps this REST call for one.
    import httpx

# The model id and the endpoint, both overridable, because a model id is the
# one thing in here most likely to change without this file being touched.
DEFAULT_MODEL = "models/gemini-3.5-transcribe"
DEFAULT_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"

# A voice note is worth about thirty seconds of patience. The bridge gives the
# whole webhook 180 s (whatsapp-bridge/index.js), and the reply itself still
# has to be written by Claude after this returns, so the transcription's whole
# budget -- first attempt, backoff, one retry -- has to stay well inside that.
# 30 + 5 + 30 leaves over a hundred seconds for the reply.
DEFAULT_TIMEOUT_SECONDS = 30.0

# How long a 429 may push us back before we give up and hold the message. The
# free tier's limits are low and Google has changed them without notice, so
# Retry-After is honoured but never trusted: a header asking for five minutes
# would otherwise hold a thread until the bridge gave up on it.
RETRY_AFTER_CAP_SECONDS = 5.0

# Raw audio, before base64. The Files API is only required once a whole request
# passes 100 MB, so inline is correct for anything WhatsApp sends -- a voice
# note is mono Opus at about 16 kbit/s, so 8 MiB is over an hour of talking,
# which the model itself will not take either (its own ceiling is one hour).
# Checked before the base64 is built, so an absurd recording costs one
# comparison rather than a 10 MiB string and a request.
MAX_AUDIO_BYTES = 8 * 1024 * 1024

# What the first bytes of a file say the container is, and what to declare it
# as. Only the types the model's own documentation lists are here; anything
# else is held rather than guessed at, exactly as app/vision.py refuses a photo
# format the Messages API would reject. WhatsApp voice notes are the first
# line; the rest are what a browser's MediaRecorder hands the dashboard.
_MAGIC_MIME_TYPES = (
    (b"OggS", "audio/ogg"),
    (b"\x1a\x45\xdf\xa3", "audio/webm"),   # EBML, i.e. WebM or Matroska
    (b"RIFF", "audio/wav"),
    (b"fLaC", "audio/flac"),
    (b"ID3", "audio/mp3"),
)

# Statuses that mean a transcript can be read off the response. Everything else
# the Interactions API can report -- in_progress, failed, cancelled,
# requires_action, requires_continuation, and incomplete, which is its word for
# a result cut short -- becomes a hold. A transcript that stopped halfway is
# the dangerous one: it reads as a whole message and is answered as one.
_USABLE_STATUS = ("completed",)


class TranscriptionUnusable(Exception):
    """This voice note could not be turned into text.

    The message is one short clause written for the owner, the way
    vision.ImageUnusable's is, because it is read beside the held message
    rather than in a log. It never carries the key, a response body, or any
    part of what was said.
    """


def _env_float(name: str, default: float) -> float:
    """Read on each call rather than at import: a getenv costs nothing beside
    an HTTP request, and it keeps the setting visible to the tests."""
    raw = os.getenv(name, "")
    try:
        value = float(raw) if raw else default
    except ValueError:
        # A typo in an env var should cost the default, not every voice note.
        log_event("gemini_stt_setting_invalid", level="warning", setting=name, value=raw)
        return default
    return value if value > 0 else default


def api_key() -> str:
    """The key, or an empty string. GEMINI_API_KEY is the name Google's own
    documentation uses, so it is the name here. Never defaulted, never
    hardcoded, never logged, and never put in a URL -- it travels in the
    x-goog-api-key header, which is where the API expects it and the one place
    it will not end up inside an exception's repr of a request."""
    return os.getenv("GEMINI_API_KEY", "").strip()


def redact(text: str) -> str:
    """Belt and braces for the one secret in this file.

    Used on the one string from outside this process that reaches a message the
    owner reads (see _transcript). Nothing else here is built out of a response
    body or an httpx repr, which is the real reason the key cannot leak -- but
    "in principle it cannot" is doing a lot of work in that sentence, and the
    next person to add a log line should have something to reach for.
    """
    key = api_key()
    return text.replace(key, "***") if key and key in text else text


def _mime_type(audio: bytes) -> str:
    """What to tell the API this audio is, read from the bytes themselves.

    The declared type is not available here: the bridge reads one off the
    WhatsApp message, but app/agent_graph.py passes the media straight to
    app/stt.py without it, and the dashboard sends whatever the browser
    recorded with no type at all. The bytes are what the API will validate
    anyway, which is the same judgement app/vision.py makes about a photo.
    """
    head = audio[:16]
    for magic, mime_type in _MAGIC_MIME_TYPES:
        if head.startswith(magic):
            return mime_type
    # ISO base media (m4a, mp4): the brand box is at offset 4, not 0.
    if audio[4:8] == b"ftyp":
        return "audio/m4a"
    # A bare MPEG audio frame: 11 set bits of sync, so 0xFF then 0xE0-0xFF.
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "audio/mp3"
    raise TranscriptionUnusable("I couldn't tell what kind of recording that was")


def _language_codes(language: str) -> list[str]:
    """BCP-47 hints for the model, or an empty list to let it listen first.

    Empty is the right default here and worth being plain about why. The
    `language` argument exists for Whisper, which cannot switch languages
    mid-clip and does measurably better when told which one to expect. This
    model's documented strength is the opposite: it detects the language itself
    and handles code-switching inside a single sentence, and Google's own
    guidance is to pass a hint only when the language is known in advance. In
    this corpus -- Kenyan English, Kiswahili and Sheng in one breath -- it
    never is, and an "en" hint is exactly the setting that made Whisper invent
    fluent English over Swahili audio.

    The one exception is a caller that has explicitly said Kiswahili, which is
    the dashboard's ?lang=sw: that is a person choosing, and sw-KE is in the
    model's language table. GEMINI_STT_LANGUAGE_CODES overrides the lot
    (comma-separated, empty string for auto).
    """
    override = os.getenv("GEMINI_STT_LANGUAGE_CODES")
    if override is not None:
        return [code.strip() for code in override.split(",") if code.strip()]
    return ["sw-KE"] if language == "sw" else []


def _request_body(audio: bytes, language: str) -> dict:
    return {
        "model": os.getenv("GEMINI_STT_MODEL", DEFAULT_MODEL),
        "input": [{
            "type": "audio",
            "data": base64.b64encode(audio).decode("ascii"),
            "mime_type": _mime_type(audio),
        }],
        "generation_config": {
            "transcription_config": {"language_codes": _language_codes(language)},
        },
    }


def _transcript(payload: object) -> str:
    """The text out of an Interactions API response, or a hold.

    Fail closed, the way llm._parse_answer does with a malformed answer:
    anything this cannot read with confidence raises instead of returning the
    empty string, because an empty string here is indistinguishable from a
    voice note nobody spoke in -- and that difference is the difference between
    "held, and here is why" and "answered as if they had said nothing".
    """
    if not isinstance(payload, dict):
        raise TranscriptionUnusable("the transcription service sent something I couldn't read")

    status = payload.get("status")
    if status not in _USABLE_STATUS:
        # The one place a string from outside this process reaches a line the
        # owner reads, so the one place redact() is not theoretical.
        raise TranscriptionUnusable("the transcription came back unfinished "
                                    f"({redact(str(status))[:30] or 'no status'})")

    pieces = []
    for step in payload.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        for content in step.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "text" \
                    and isinstance(content.get("text"), str):
                pieces.append(content["text"].strip())

    text = " ".join(piece for piece in pieces if piece).strip()
    if not text:
        raise TranscriptionUnusable("the transcription came back empty")
    return text


def _retry_after_seconds(headers: httpx.Headers | None) -> float:
    """How long a 429 or a 503 asked us to wait, bounded, with jitter.

    Jitter because Google's own retry guidance asks for it, and bounded
    because an unbounded Retry-After is a thread held until the bridge times
    out. A header we cannot read is not an error -- it just means we pick the
    wait ourselves.
    """
    wait = 2.0
    try:
        asked = float(headers.get("retry-after", ""))
        if asked > 0:
            wait = asked
    except (AttributeError, TypeError, ValueError):
        pass
    return min(wait, RETRY_AFTER_CAP_SECONDS) + random.uniform(0, 0.5)


def _post(body: dict, key: str) -> tuple[int, httpx.Headers, object]:
    """One request. Returns (status, headers, parsed json or None).

    httpx is imported here rather than at the top of the file -- see the
    TYPE_CHECKING block. A client per call rather than one kept alive: a voice
    note arrives every few minutes at most, so the second TLS handshake costs
    less than a pool of idle sockets on a host that is already swapping, and
    there is no global left holding a key.
    """
    import httpx

    timeout = _env_float("GEMINI_STT_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
    endpoint = os.getenv("GEMINI_STT_ENDPOINT", DEFAULT_ENDPOINT)
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            endpoint,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json=body,
        )
        try:
            payload = response.json()
        except ValueError:
            # A 500 behind a proxy is often HTML. The status is what matters
            # from here; the body is deliberately not kept or logged.
            payload = None
        return response.status_code, response.headers, payload


def transcribe(audio: bytes, language: str = "auto") -> str:
    """Audio bytes to text, or TranscriptionUnusable with a reason for the owner.

    One retry at most, and only for the failures Google's own guidance calls
    transient (429, 408, 5xx). Three things deliberately get no retry:

      * a timeout, because it has already spent its whole budget and a second
        attempt is what would push the webhook past the bridge's 180 s;
      * 401 and 403, because a refused key will be refused again;
      * 400, because a request this code built wrongly will be built wrongly
        again, and the owner needs to see it rather than wait through it.

    Anything still failing after that is held. There is no fall back to local
    Whisper here, which is a decision with a cost, argued in app/stt.py where
    the choice between providers is made.
    """
    # The deferred import, here as well as in _post, because the except
    # clauses below need the real exception classes: telling a transport
    # failure apart by the name of its type would quietly turn a bug in this
    # file into "I couldn't reach the transcription service", which is both a
    # lie to the owner and a bug nobody ever finds. sys.modules makes the
    # second import free.
    import httpx

    key = api_key()
    if not key:
        # The caller is supposed to have checked (stt.use_gemini does). Raising
        # rather than quietly transcribing nothing, because reaching here means
        # a caller is wrong, not that a voice note was bad.
        raise TranscriptionUnusable("no transcription key is configured")
    if not audio:
        raise TranscriptionUnusable("the recording didn't come through")
    if len(audio) > MAX_AUDIO_BYTES:
        raise TranscriptionUnusable("that recording is too long to transcribe")

    body = _request_body(audio, language)
    started = time.monotonic()
    payload: object = None

    for attempt in (1, 2):
        try:
            status, headers, payload = _post(body, key)
        # Only httpx's own failures. InvalidURL is named beside HTTPError
        # because it is not one of its subclasses, and a typo in
        # GEMINI_STT_ENDPOINT would otherwise be the one misconfiguration that
        # reaches the webhook as a 500. Both messages are built from the type
        # name alone: a transport error's text can carry the URL it was
        # reaching for, and nothing from outside this process belongs in a
        # line the owner reads.
        except (httpx.HTTPError, httpx.InvalidURL) as err:
            log_event("gemini_stt_failed", level="warning", error=type(err).__name__,
                      attempt=attempt, kb=len(audio) // 1024)
            if isinstance(err, httpx.TimeoutException):
                raise TranscriptionUnusable(
                    "the transcription service didn't answer in time") from None
            raise TranscriptionUnusable(
                "I couldn't reach the transcription service") from None

        if status == 200:
            break
        retryable = status in (408, 429) or 500 <= status < 600
        log_event("gemini_stt_rejected", level="warning", status=status,
                  attempt=attempt, retryable=retryable, kb=len(audio) // 1024)
        if not retryable:
            if status in (401, 403):
                raise TranscriptionUnusable("the transcription key was refused")
            raise TranscriptionUnusable(
                f"the transcription service refused that recording ({status})")
        if attempt == 2:
            if status == 429:
                raise TranscriptionUnusable(
                    "the free transcription quota is used up for now")
            raise TranscriptionUnusable("the transcription service is having trouble")
        time.sleep(_retry_after_seconds(headers))

    text = _transcript(payload)
    # Counts only: how long it took, how big the audio was, how much text came
    # back. Never the audio, never a word of the transcript, never the key.
    log_event("gemini_stt", ms=int((time.monotonic() - started) * 1000),
              kb=len(audio) // 1024, chars=len(text))
    return text
