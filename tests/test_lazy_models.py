"""The heavy libraries must stay out of a process that never uses them, and a
model loaded for one voice note must not hold its memory for the rest of the
week.

Both of those are facts about the deployed container's memory, so they are
checked by measuring rather than by reading the source: the import test runs in
a subprocess, because this process has imported plenty by the time it runs, and
the eviction tests drive the real app/stt.py with a stand-in for the model
class — the suite is meant to stay offline and deterministic, and a real
"small" Whisper is 610 MiB.

The same discipline covers the remote transcription provider: app/stt_gemini.py
is reached through a function in app/stt.py, so a backend that answers text
messages imports neither it nor the Google SDK somebody may one day replace its
REST call with. tests/test_stt_gemini.py tests what that provider does; this
file only holds the line about when it is imported.

There used to be a second half to this file, for BLIP. The captioner is gone
(photos are now sent to the model as images), and with it torch and
transformers; Pillow is the one library left that app/vision.py must not
import until a photo actually arrives.
"""
import importlib.util
import os
import subprocess
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HEAVY = ("faster_whisper", "ctranslate2", "PIL")

# The same rule, for the transcription provider that is not local. app/stt.py
# reaches app/stt_gemini.py through a function, so neither the provider module
# nor anything it might one day import is in a process that only answers text
# messages. The Google names are listed although nothing imports them today:
# this is the test that has to fail the day somebody "tidies up" the REST call
# in app/stt_gemini.py into google-genai, which would put google-auth,
# protobuf and grpcio in the boot path of every backend.
#
# "google" is the namespace package every Google library installs into, so
# naming it covers the SDKs that do not exist yet as well as the ones that do.
REMOTE = ("app.stt_gemini", "google", "google.genai", "google.generativeai",
          "googleapiclient", "grpc", "proto")


def _load_real_module(name: str):
    """app/<name>.py under its own module name.

    tests/conftest.py replaces app.stt in sys.modules with a light stub so the
    rest of the suite needs no models; these tests are about the real file, so
    a plain import would hand back the stub.
    """
    spec = importlib.util.spec_from_file_location(f"real_{name}", REPO_ROOT / "app" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_importing_the_agent_graph_does_not_import_the_heavy_libraries():
    """app/main.py imports app.agent_graph, which imports app.stt and app.vision.
    When those two did their imports at module level, every backend paid about
    220 MiB of torch, transformers and ctranslate2 from boot — including one
    that only ever answers text messages. torch and transformers are gone now;
    ctranslate2 and Pillow are what is left to keep out of the boot path."""
    code = (
        "import sys\n"
        "import app.agent_graph\n"
        f"print(','.join(m for m in {HEAVY!r} if m in sys.modules))\n"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "", f"imported from boot: {done.stdout.strip()}"


def test_importing_the_app_does_not_import_a_transcription_provider():
    """Measured in a subprocess for the same reason as the test above, and
    through app.main rather than app.agent_graph because app.main is what the
    container runs and what imports app.stt directly as well.

    STT_PROVIDER is set to gemini here on purpose: the provider being switched
    on must not be the thing that decides when its module is imported, or the
    saving quietly disappears on the one deployment that uses it."""
    code = (
        "import sys\n"
        "import app.main\n"
        f"print(','.join(m for m in {REMOTE!r} if m in sys.modules))\n"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120,
                          env={**os.environ, "STT_PROVIDER": "gemini",
                               "GEMINI_API_KEY": "not-a-real-key"})
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "", f"imported from boot: {done.stdout.strip()}"


@pytest.fixture()
def whisper(monkeypatch):
    loaded = []

    class FakeWhisperModel:
        def __init__(self, size, device=None, compute_type=None):
            self.size = size
            loaded.append(size)

    fake = types.ModuleType("faster_whisper")
    fake.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    monkeypatch.setenv("MODEL_IDLE_MINUTES", "15")
    return SimpleNamespace(stt=_load_real_module("stt"), loads=loaded)


def test_asking_for_no_size_follows_whisper_model_size(whisper):
    """docker-compose.yaml leaves DASHBOARD_WHISPER_SIZE empty so that the
    dashboard and the webhook share one loaded model instead of holding two
    sets of weights. app/dashboard.py turns that empty value into None; this is
    the other end of that path, and the reason the compose comment can promise
    one model."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("WHISPER_MODEL_SIZE", "base")
        assert whisper.stt._acquire_model(None)[0] == "base"
        whisper.stt._release_model("base")
        # A size the caller did name still wins, which is what opting into a
        # second model looks like.
        assert whisper.stt._acquire_model("small")[0] == "small"

    assert whisper.loads == ["base", "small"]


def test_the_default_size_matches_the_one_docker_compose_deploys(whisper):
    """With nothing in the environment the code has to land on the same size
    the deployed container asks for, or a local run and the VPS disagree about
    how much memory a voice note costs — 139 MiB of weights against 461."""
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("WHISPER_MODEL_SIZE", raising=False)
        assert whisper.stt._acquire_model(None)[0] == "base"


def test_a_model_used_moments_ago_is_not_evicted(whisper):
    size, model = whisper.stt._acquire_model("small")
    whisper.stt._release_model(size)

    assert whisper.stt._acquire_model("small")[1] is model
    assert whisper.loads == ["small"], "it reloaded a model it had just used"


def test_a_model_idle_past_the_window_is_dropped_and_reloaded(whisper):
    stt = whisper.stt
    size, first = stt._acquire_model("small")
    stt._release_model(size)

    # Backdated rather than slept through: the clock is the only input to the
    # decision, and a 15-minute test is no test.
    stt._last_used["small"] = time.monotonic() - 16 * 60
    size, second = stt._acquire_model("small")

    assert second is not first
    assert whisper.loads == ["small", "small"]
    assert stt._in_use == {"small": 1} and list(stt._models) == ["small"]


def test_an_idle_model_does_not_take_a_fresh_one_with_it(whisper):
    stt = whisper.stt
    stt._release_model(stt._acquire_model("small")[0])
    stt._release_model(stt._acquire_model("medium")[0])

    # The dashboard can be set to a different size from WhatsApp's, so these
    # two caches go idle on their own schedules.
    stt._last_used["small"] = time.monotonic() - 16 * 60
    stt._acquire_model("medium")

    assert list(stt._models) == ["medium"]


def test_a_model_in_use_is_never_evicted(whisper):
    stt = whisper.stt
    # Two overlapping transcriptions: /webhook hands the agent to a thread pool
    # and the dashboard dictates in another, so the first caller may still be
    # inside the model when the second arrives.
    _, held = stt._acquire_model("small")
    stt._last_used["small"] = time.monotonic() - 60 * 60

    assert stt._acquire_model("small")[1] is held
    assert whisper.loads == ["small"], "it dropped a model a thread was using"


def test_the_window_can_be_switched_off(whisper):
    stt = whisper.stt
    stt._release_model(stt._acquire_model("small")[0])
    stt._last_used["small"] = time.monotonic() - 24 * 60 * 60

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MODEL_IDLE_MINUTES", "0")
        stt._acquire_model("small")

    assert whisper.loads == ["small"]


def test_a_nonsense_idle_window_falls_back_to_the_default(whisper):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MODEL_IDLE_MINUTES", "soon")
        assert whisper.stt.idle_window_seconds() == whisper.stt.DEFAULT_IDLE_MINUTES * 60
