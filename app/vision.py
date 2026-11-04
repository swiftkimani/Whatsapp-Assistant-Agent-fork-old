"""Image captioning with BLIP."""
from __future__ import annotations

import base64
import ctypes
import gc
import io
import os
import sys
import threading
import time
from typing import TYPE_CHECKING

from app.log import log_event

if TYPE_CHECKING:
    # Only a type checker ever executes this. The real import lives in
    # _acquire, because transformers pulls torch in behind it for about
    # 180 MiB that a backend which never receives a photo used to pay from
    # boot. "from __future__ import annotations" at the top is what makes the
    # globals below safe to annotate: a module-level annotation is evaluated
    # at import time, so these two names would otherwise import the library
    # the moment this file is read.
    from transformers import BlipForConditionalGeneration, BlipProcessor

MODEL_NAME = "Salesforce/blip-image-captioning-base"

# The same window app/stt.py uses: fifteen minutes of nobody sending a photo is
# long enough to be sure the gigabyte is not about to be needed again.
DEFAULT_IDLE_MINUTES = 15.0

_processor: BlipProcessor | None = None
_model: BlipForConditionalGeneration | None = None
# When the model was last finished with, and how many threads are inside it
# right now, so an eviction can never drop a model mid-caption. /webhook runs
# the agent in a thread pool, so two photos really can overlap.
_last_used = 0.0
_in_use = 0
_lock = threading.Lock()


def idle_window_seconds() -> float:
    """How long the model may sit unused before the next caption drops it.

    MODEL_IDLE_MINUTES=0 keeps the old behaviour of never evicting. The same
    few lines live in app/stt.py: importing them from there would make this
    module depend on that one for an unrelated reason, and tests/conftest.py
    replaces app.stt with a stub that would then have to grow this name too.
    A third copy is the point at which this belongs in a module of its own.
    """
    raw = os.getenv("MODEL_IDLE_MINUTES", "")
    try:
        minutes = float(raw) if raw else DEFAULT_IDLE_MINUTES
    except ValueError:
        # A typo in an env var should cost the default, not every caption.
        log_event("model_idle_minutes_invalid", level="warning", value=raw)
        minutes = DEFAULT_IDLE_MINUTES
    return max(minutes, 0.0) * 60


def _return_free_pages() -> None:
    """Hand the freed weights back to the operating system, not just to Python.

    The weights hang off a graph of torch modules, so anything left alive in a
    reference cycle would pay the reload *and* keep the gigabyte; hence the
    collection. malloc_trim then asks glibc to return the freed arenas rather
    than keeping them for reuse, which is the retention MALLOC_ARENA_MAX=2 in
    docker-compose.yaml is already about. Best effort by design — see the twin
    of this function in app/stt.py.
    """
    gc.collect()
    if sys.platform != "linux":
        return
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass  # not glibc, or a build without it: the collection above still stands


def _evict_if_idle(now: float) -> None:
    """Drop the weights if nothing has captioned an image for the window.

    Called with _lock held, and only from _acquire — see the longer note in
    app/stt.py for why there is no background thread doing this and what that
    costs. BLIP in fp32 is about 1 GB, so this is the larger of the two
    savings and also the slower reload: a photo arriving after a quiet stretch
    pays the load again rather than the host paying for the whole gap.
    """
    global _processor, _model
    window = idle_window_seconds()
    if not window or _model is None or _in_use or now - _last_used < window:
        return
    log_event("blip_model_evicted", idle_minutes=round((now - _last_used) / 60, 1))
    _processor = _model = None
    _return_free_pages()


def _acquire() -> tuple[BlipProcessor, BlipForConditionalGeneration]:
    """The caller must call _release when it is done, or the model can never be
    evicted again."""
    global _processor, _model, _in_use, _last_used
    with _lock:
        now = time.monotonic()
        _evict_if_idle(now)
        if _model is None:
            from transformers import BlipForConditionalGeneration, BlipProcessor

            _processor = BlipProcessor.from_pretrained(MODEL_NAME)
            # Loaded in fp32 on purpose, after checking both of the obvious
            # ways to halve it. low_cpu_mem_usage=True would lower the peak
            # during the load, but transformers 4.44.2 (the pin in
            # requirements.txt) raises ImportError unless `accelerate` is
            # installed, and it is not a dependency of this project or of
            # transformers — so passing it would turn every photo into a 500.
            # torch_dtype=float16 is the other halving and is the wrong trade
            # on this box: the VPS is CPU-only, where many fp16 kernels either
            # fall back to fp32 anyway or are not implemented at all, so it
            # buys a slower caption for an uncertain saving. Idle eviction
            # below is what actually gives the gigabyte back.
            _model = BlipForConditionalGeneration.from_pretrained(MODEL_NAME)
        _in_use += 1
        _last_used = now
        return _processor, _model


def _release() -> None:
    global _in_use, _last_used
    with _lock:
        _in_use = max(_in_use - 1, 0)
        # Idle time runs from the end of a caption, not the start.
        _last_used = time.monotonic()


def caption_base64_image(media_base64: str) -> str:
    # Pillow is 16 MiB and only this path ever needs it, which is the same
    # reason transformers is imported inside _acquire.
    from PIL import Image

    processor, model = _acquire()
    try:
        image = Image.open(io.BytesIO(base64.b64decode(media_base64))).convert("RGB")
        inputs = processor(image, return_tensors="pt")
        out = model.generate(**inputs, max_new_tokens=30)
        return processor.decode(out[0], skip_special_tokens=True)
    finally:
        _release()
