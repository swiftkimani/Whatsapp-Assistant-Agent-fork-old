"""The remote transcription provider, and the seam it sits behind.

Offline and deterministic like the rest of the suite: no key is ever used, no
request ever leaves, and the clock never really sleeps. The stand-in goes in at
the HTTP boundary -- sys.modules["httpx"], the same trick tests/test_lazy_models
plays with faster_whisper -- rather than at app.stt_gemini.transcribe, because
what most of these tests are actually about is the request that goes out and
the header the key travels in, and a stub at the function boundary would assert
neither.

The two properties worth saying out loud, since they are what a voice note's
privacy and a sender's reply depend on:

  * a transcription that fails is HELD, never answered and never a crash;
  * nothing about the audio, the words, or the key reaches a log line.
"""
import base64
import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Not a real key and not a real shape, but distinctive enough that a test can
# search a whole log file for it and mean something.
FAKE_KEY = "AIza-this-key-must-never-be-logged-0001"
# "OggS" is what a WhatsApp voice note starts with, which is the whole reason
# app/stt_gemini.py reads the container out of the bytes.
OGG = b"OggS" + bytes(60)
HEARD = "niaje manze, nakuja kwa base saa tatu"


def _load_real_stt():
    """app/stt.py under its own name.

    tests/conftest.py replaces app.stt in sys.modules with a light stub so the
    rest of the suite needs no models; these tests are about the real file, and
    a plain import would hand back the stub. Each call builds a fresh module,
    which also resets the "said once" bookkeeping inside it.
    """
    spec = importlib.util.spec_from_file_location("real_stt", REPO_ROOT / "app" / "stt.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def completed(text: str) -> dict:
    """An Interactions API response, shaped the way the reference documents it:
    https://ai.google.dev/api/interactions-api (Interaction, Step, Content)."""
    return {"id": "v1_test", "status": "completed", "model": "gemini-3.5-transcribe",
            "steps": [{"type": "model_output",
                       "content": [{"type": "text", "text": text}]}]}


class FakeResponse:
    def __init__(self, status_code: int, payload=None, headers=None, bad_json=False):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")   # a 500 behind a proxy is usually HTML
        return self._payload


@pytest.fixture()
def whisper(monkeypatch):
    """A stand-in for the local model, so the fallback path can be exercised
    without 138 MiB of weights. Same approach as tests/test_lazy_models.py."""
    loaded = []

    class FakeWhisperModel:
        def __init__(self, size, device=None, compute_type=None):
            loaded.append(size)

        def transcribe(self, path, language=None, vad_filter=False):
            segment = SimpleNamespace(text=" heard locally ")
            return [segment], SimpleNamespace(language=language or "en", all_language_probs=[])

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return loaded


@pytest.fixture()
def gemini(monkeypatch, whisper):
    """The provider switched on, with the network replaced.

    `scripted` is what the next request will return (a FakeResponse) or raise
    (an exception instance); it defaults to a successful transcription so that
    a test only has to say what it is interested in. `calls` is every request
    that was made, which is where the assertions about the body and the header
    come from.
    """
    calls: list[dict] = []
    scripted: list[object] = []
    slept: list[float] = []

    # The real hierarchy, because app/stt_gemini.py catches these by class and
    # not by name: HTTPError is the base for transport and status failures,
    # and InvalidURL deliberately is not one of its subclasses.
    class HTTPError(Exception):
        pass

    class RequestError(HTTPError):
        pass

    class TimeoutException(RequestError):
        pass

    class InvalidURL(Exception):
        pass

    class Client:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def post(self, url, headers=None, json=None):
            calls.append({"url": url, "headers": headers or {}, "body": json,
                          "timeout": self.timeout})
            nxt = scripted.pop(0) if scripted else FakeResponse(200, completed(HEARD))
            if isinstance(nxt, BaseException):
                raise nxt
            return nxt

    fake_httpx = types.ModuleType("httpx")
    fake_httpx.Client = Client
    fake_httpx.HTTPError = HTTPError
    fake_httpx.RequestError = RequestError
    fake_httpx.TimeoutException = TimeoutException
    fake_httpx.InvalidURL = InvalidURL
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

    monkeypatch.setenv("STT_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    monkeypatch.delenv("GEMINI_STT_LANGUAGE_CODES", raising=False)

    stt = _load_real_stt()
    provider = stt._gemini()
    # The backoff is real seconds in production and nothing in a test. Patched
    # on the time module itself because that is the object app/stt_gemini.py
    # holds; pytest puts it back afterwards.
    monkeypatch.setattr(provider.time, "sleep", slept.append)

    return SimpleNamespace(stt=stt, provider=provider, calls=calls,
                           scripted=scripted, slept=slept, whisper_loads=whisper)


class LogTail:
    """Everything app/log.py wrote since this was created.

    The log file is the real one the whole suite shares (conftest.py points
    APP_LOG_FILE at a temp file), so it is read by offset rather than whole.
    """

    def __init__(self):
        self.path = Path(os.environ["APP_LOG_FILE"])
        self.offset = self.path.stat().st_size if self.path.exists() else 0

    def text(self) -> str:
        if not self.path.exists():
            return ""
        return self.path.read_text(encoding="utf-8")[self.offset:]

    def events(self) -> list[str]:
        return [json.loads(line)["event"] for line in self.text().splitlines() if line.strip()]


# ------------------------------------------------------- off, or not configured

def test_with_nothing_in_the_environment_the_local_model_transcribes(whisper, monkeypatch):
    monkeypatch.delenv("STT_PROVIDER", raising=False)
    stt = _load_real_stt()

    assert stt.provider() == "whisper"
    assert stt.use_gemini() is False
    assert stt.transcribe_base64_audio(base64.b64encode(OGG).decode()) == "heard locally"
    assert whisper == ["base"]


def test_with_no_key_the_local_model_transcribes_and_says_so_once(whisper, monkeypatch):
    """A key that has not arrived yet must not break a single message, and must
    not warn about itself on every voice note for the rest of the week."""
    monkeypatch.setenv("STT_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    stt = _load_real_stt()
    log = LogTail()

    for _ in range(3):
        assert stt.transcribe_base64_audio(base64.b64encode(OGG).decode()) == "heard locally"

    assert log.events().count("stt_provider_has_no_key") == 1
    assert whisper == ["base"], "it loaded the local model more than once"


def test_a_blank_key_counts_as_no_key(whisper, monkeypatch):
    """Coolify writes an empty field as an empty string, not as nothing."""
    monkeypatch.setenv("STT_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    stt = _load_real_stt()

    assert stt.use_gemini() is False
    assert stt.transcribe_base64_audio(base64.b64encode(OGG).decode()) == "heard locally"


# --------------------------------------------------------------- the request

def test_a_voice_note_reaches_gemini_as_the_audio_that_arrived(gemini):
    text = gemini.stt.transcribe_base64_audio(base64.b64encode(OGG).decode())

    assert text == HEARD
    assert len(gemini.calls) == 1
    assert gemini.whisper_loads == [], "it loaded a local model it was not going to use"

    body = gemini.calls[0]["body"]
    assert body["model"] == "models/gemini-3.5-transcribe"
    assert gemini.calls[0]["url"].endswith("/v1beta/interactions")
    audio_part, = body["input"]
    assert audio_part["type"] == "audio"
    assert base64.b64decode(audio_part["data"]) == OGG
    assert audio_part["mime_type"] == "audio/ogg"


def test_the_key_travels_in_a_header_and_never_in_the_url(gemini):
    gemini.stt.transcribe_base64_audio(base64.b64encode(OGG).decode())

    call = gemini.calls[0]
    assert call["headers"]["x-goog-api-key"] == FAKE_KEY
    assert FAKE_KEY not in call["url"]
    assert FAKE_KEY not in json.dumps(call["body"])


def test_the_timeout_is_set_on_the_call_and_can_be_changed(gemini, monkeypatch):
    """Every outbound call gets a timeout, and this one has to stay well inside
    the 180s the bridge allows the whole webhook."""
    gemini.stt.transcribe_base64_audio(base64.b64encode(OGG).decode())
    assert gemini.calls[0]["timeout"] == 30.0

    monkeypatch.setenv("GEMINI_STT_TIMEOUT", "12.5")
    gemini.stt.transcribe_base64_audio(base64.b64encode(OGG).decode())
    assert gemini.calls[1]["timeout"] == 12.5


def test_a_nonsense_timeout_falls_back_to_the_default(gemini, monkeypatch):
    monkeypatch.setenv("GEMINI_STT_TIMEOUT", "soon")
    gemini.stt.transcribe_base64_audio(base64.b64encode(OGG).decode())

    assert gemini.calls[0]["timeout"] == 30.0


def test_anything_but_a_deliberate_kiswahili_lets_the_model_listen_first(gemini):
    """Auto-detection is the reason this provider exists: the corpus switches
    language inside a sentence, and an "en" hint is what made Whisper invent
    English over Swahili audio."""
    gemini.provider.transcribe(OGG, language="en")
    gemini.provider.transcribe(OGG, language="auto")
    gemini.provider.transcribe(OGG, language="sw")

    codes = [call["body"]["generation_config"]["transcription_config"]["language_codes"]
             for call in gemini.calls]
    assert codes == [[], [], ["sw-KE"]]


def test_the_language_codes_can_be_overridden(gemini, monkeypatch):
    monkeypatch.setenv("GEMINI_STT_LANGUAGE_CODES", "sw-KE, en-GB")
    gemini.provider.transcribe(OGG, language="en")

    config = gemini.calls[0]["body"]["generation_config"]["transcription_config"]
    assert config["language_codes"] == ["sw-KE", "en-GB"]


def test_each_container_is_declared_as_the_type_the_model_accepts(gemini):
    """The bridge's declared mimetype never reaches app/stt.py, and the
    dashboard sends whatever the browser recorded, so the bytes decide."""
    assert gemini.provider._mime_type(OGG) == "audio/ogg"
    assert gemini.provider._mime_type(b"\x1a\x45\xdf\xa3" + bytes(20)) == "audio/webm"
    assert gemini.provider._mime_type(b"RIFF" + bytes(4) + b"WAVE") == "audio/wav"
    assert gemini.provider._mime_type(bytes(4) + b"ftypM4A " + bytes(8)) == "audio/m4a"
    assert gemini.provider._mime_type(b"ID3" + bytes(20)) == "audio/mp3"
    assert gemini.provider._mime_type(b"\xff\xfb" + bytes(20)) == "audio/mp3"


# ------------------------------------------------------------ failure, held

def test_a_rate_limited_request_is_retried_once_and_then_held(gemini):
    """The free tier's limits are low and Google changes them without notice,
    so a 429 is an ordinary answer. One retry, then the owner gets the message."""
    gemini.scripted.extend([FakeResponse(429, headers={"retry-after": "1"}),
                            FakeResponse(429, headers={"retry-after": "1"})])

    with pytest.raises(gemini.provider.TranscriptionUnusable) as held:
        gemini.provider.transcribe(OGG)

    assert len(gemini.calls) == 2, "it retried more than once, or not at all"
    assert "quota" in str(held.value)
    assert gemini.slept == [pytest.approx(1.0, abs=0.5)]


def test_a_request_that_succeeds_on_the_retry_is_not_held(gemini):
    gemini.scripted.extend([FakeResponse(429), FakeResponse(200, completed(HEARD))])

    assert gemini.provider.transcribe(OGG) == HEARD
    assert len(gemini.calls) == 2


def test_a_long_retry_after_cannot_hold_the_webhook_open(gemini):
    """A header asking for five minutes would otherwise hold a thread until the
    bridge gave up on the whole message."""
    gemini.scripted.extend([FakeResponse(429, headers={"retry-after": "300"}),
                            FakeResponse(200, completed(HEARD))])

    gemini.provider.transcribe(OGG)

    assert gemini.slept[0] <= gemini.provider.RETRY_AFTER_CAP_SECONDS + 0.5


def test_a_server_error_is_retried_once_and_then_held(gemini):
    gemini.scripted.extend([FakeResponse(503, bad_json=True), FakeResponse(503, bad_json=True)])

    with pytest.raises(gemini.provider.TranscriptionUnusable):
        gemini.provider.transcribe(OGG)
    assert len(gemini.calls) == 2


def test_a_timeout_is_held_without_a_second_attempt(gemini):
    """A timeout has already spent its whole budget; a second 30s attempt is
    what would push the webhook past the bridge's ceiling."""
    import httpx

    gemini.scripted.append(httpx.TimeoutException("timed out"))

    with pytest.raises(gemini.provider.TranscriptionUnusable) as held:
        gemini.provider.transcribe(OGG)

    assert len(gemini.calls) == 1
    assert "in time" in str(held.value)
    assert gemini.slept == []


def test_an_unreachable_service_is_held_without_a_second_attempt(gemini):
    import httpx

    gemini.scripted.append(httpx.RequestError("no route to host"))

    with pytest.raises(gemini.provider.TranscriptionUnusable) as held:
        gemini.provider.transcribe(OGG)
    assert len(gemini.calls) == 1
    assert "reach" in str(held.value)


def test_a_typo_in_the_endpoint_is_held_rather_than_a_500_in_the_webhook(gemini):
    """httpx.InvalidURL is not an HTTPError, so a misconfigured
    GEMINI_STT_ENDPOINT would otherwise be the one setting that reaches the
    webhook as a traceback."""
    import httpx

    gemini.scripted.append(httpx.InvalidURL("no host"))

    with pytest.raises(gemini.provider.TranscriptionUnusable):
        gemini.provider.transcribe(OGG)


def test_a_bug_inside_the_request_is_not_dressed_up_as_a_network_failure(gemini, monkeypatch):
    """The transport failures are caught by class, not by the name of the
    type, so a mistake in this file stays loud instead of becoming "I couldn't
    reach the transcription service" forever."""
    def exploding_post(body, key):
        raise TypeError("a bug, not a network")

    monkeypatch.setattr(gemini.provider, "_post", exploding_post)

    with pytest.raises(TypeError):
        gemini.provider.transcribe(OGG)


def test_a_refused_key_is_held_without_a_retry_and_without_naming_the_key(gemini):
    gemini.scripted.append(FakeResponse(403, {"error": {"message": f"key {FAKE_KEY} denied"}}))

    with pytest.raises(gemini.provider.TranscriptionUnusable) as held:
        gemini.provider.transcribe(OGG)

    assert len(gemini.calls) == 1, "it retried a key that will be refused again"
    assert FAKE_KEY not in str(held.value)


def test_a_rejected_request_is_held_without_a_retry(gemini):
    gemini.scripted.append(FakeResponse(400, {"error": {"message": "bad request"}}))

    with pytest.raises(gemini.provider.TranscriptionUnusable):
        gemini.provider.transcribe(OGG)
    assert len(gemini.calls) == 1


@pytest.mark.parametrize("payload", [
    None,                                                         # unparseable body
    "a string where an object belongs",
    {},                                                           # no status at all
    {"status": "completed"},                                      # no steps
    {"status": "completed", "steps": []},
    {"status": "completed", "steps": [{"type": "model_output", "content": []}]},
    {"status": "completed", "steps": [{"type": "model_output",
                                       "content": [{"type": "text", "text": "   "}]}]},
    {"status": "failed", "steps": [{"type": "model_output",
                                    "content": [{"type": "text", "text": "half a"}]}]},
    {"status": "incomplete", "steps": [{"type": "model_output",
                                        "content": [{"type": "text", "text": "half a"}]}]},
])
def test_a_response_with_no_transcript_in_it_is_held_not_read_as_silence(gemini, payload):
    """The dangerous outcome is not an error, it is an empty string: that is
    indistinguishable from a voice note nobody spoke in, and it is the
    difference between "held, and here is why" and "answered as if they said
    nothing". `incomplete` is in here on purpose -- a transcript cut halfway
    reads as a whole message and would be answered as one."""
    gemini.scripted.append(FakeResponse(200, payload))

    with pytest.raises(gemini.provider.TranscriptionUnusable) as held:
        gemini.provider.transcribe(OGG)
    assert str(held.value), "a hold with no reason is no use to the owner"


def test_a_recording_in_an_unknown_container_is_held_before_any_request(gemini):
    with pytest.raises(gemini.provider.TranscriptionUnusable):
        gemini.provider.transcribe(b"\x89PNG\r\n\x1a\n" + bytes(20))
    assert gemini.calls == []


def test_a_recording_too_long_to_send_is_held_before_any_request(gemini):
    too_big = OGG + bytes(gemini.provider.MAX_AUDIO_BYTES)

    with pytest.raises(gemini.provider.TranscriptionUnusable):
        gemini.provider.transcribe(too_big)
    assert gemini.calls == [], "it base64-encoded 8 MiB before refusing it"


def test_nothing_at_all_is_held_before_any_request(gemini):
    with pytest.raises(gemini.provider.TranscriptionUnusable):
        gemini.provider.transcribe(b"")
    assert gemini.calls == []


# ------------------------------------------------- what the callers still get

def test_a_failed_transcription_is_an_empty_string_not_an_exception(gemini):
    """transcribe_base64_audio's contract, which app/agent_graph.py depends on:
    base64 in, text out. The empty string is what app/llm.py refuses to call
    the model with, so verify() holds the message -- the same path a voice note
    Whisper heard silence in has always taken. A raise here would be a 500 in
    the webhook instead."""
    gemini.scripted.extend([FakeResponse(429), FakeResponse(429)])

    assert gemini.stt.transcribe_base64_audio(base64.b64encode(OGG).decode()) == ""
    assert gemini.whisper_loads == [], "it fell back to the model that hallucinates"


def test_a_failure_leaves_a_reason_in_the_log_for_the_owner(gemini):
    gemini.scripted.extend([FakeResponse(429), FakeResponse(429)])
    log = LogTail()

    gemini.stt.transcribe_base64_audio(base64.b64encode(OGG).decode())

    held = [json.loads(line) for line in log.text().splitlines()
            if line.strip() and json.loads(line)["event"] == "stt_unusable"]
    assert len(held) == 1
    assert held[0]["reason"], "held with no reason"
    assert held[0]["provider"] == "gemini"


def test_a_real_bug_still_raises_rather_than_becoming_a_silent_hold(gemini, monkeypatch):
    """Only the provider's own typed failure is caught. Swallowing everything
    would turn a mistake in this code into every voice note quietly becoming a
    hold, with nobody the wiser."""
    monkeypatch.setattr(gemini.provider, "_request_body",
                        lambda audio, language: (_ for _ in ()).throw(KeyError("a bug")))

    with pytest.raises(KeyError):
        gemini.stt.transcribe_base64_audio(base64.b64encode(OGG).decode())


def test_the_dashboards_whisper_arguments_are_still_accepted(gemini):
    """app/dashboard.py calls transcribe_bytes(audio, lang, size, True)
    positionally. Switching provider must not change what that means to a
    caller, even though a remote model has no size to pick and no VAD filter."""
    assert gemini.stt.transcribe_bytes(OGG, "auto", "small", True) == HEARD
    assert gemini.whisper_loads == []


# ----------------------------------------------------------------- the logs

def test_neither_the_audio_nor_the_transcript_reaches_the_log(gemini):
    log = LogTail()

    gemini.stt.transcribe_base64_audio(base64.b64encode(OGG).decode())

    written = log.text()
    assert "gemini_stt" in log.events()
    assert HEARD not in written
    for word in HEARD.split():
        assert word not in written
    assert base64.b64encode(OGG).decode() not in written
    # Counts, though: a log with nothing measurable in it is not the goal.
    assert any(json.loads(line).get("chars") == len(HEARD)
               for line in written.splitlines() if line.strip())


def test_the_key_never_appears_in_a_log_line(gemini):
    """Every path, not just the happy one: a refusal is where a key would most
    plausibly end up, since that is when a service wants to talk about it."""
    log = LogTail()
    gemini.scripted.extend([FakeResponse(403, {"error": {"message": f"bad key {FAKE_KEY}"}}),
                            FakeResponse(429, headers={"retry-after": "1"}),
                            FakeResponse(429, headers={"retry-after": "1"}),
                            FakeResponse(200, completed(HEARD))])

    for _ in range(3):
        gemini.stt.transcribe_base64_audio(base64.b64encode(OGG).decode())

    assert FAKE_KEY not in log.text()


def test_the_redaction_takes_the_key_out_of_anything_it_is_given(gemini):
    """Nothing here builds a message out of a response body, which is why the
    test above passes. This is the guard for the next line somebody adds."""
    assert FAKE_KEY not in gemini.provider.redact(f"refused: {FAKE_KEY}")
    assert gemini.provider.redact("nothing to redact") == "nothing to redact"
