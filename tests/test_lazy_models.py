"""The heavy libraries must stay out of a process that never uses them, and a
model loaded for one voice note must not hold its memory for the rest of the
week.

Both of those are facts about the deployed container's memory, so they are
checked by measuring rather than by reading the source: the import test runs in
a subprocess, because this process has imported plenty by the time it runs, and
the eviction tests drive the real app/stt.py with a stand-in for the model
class — the suite is meant to stay offline and deterministic, and a real
"small" Whisper is 610 MiB.

There used to be a second half to this file, for BLIP. The captioner is gone
(photos are now sent to the model as images), and with it torch and
transformers; Pillow is the one library left that app/vision.py must not
import until a photo actually arrives.
"""
import importlib.util
import subprocess
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HEAVY = ("faster_whisper", "ctranslate2", "PIL")


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
