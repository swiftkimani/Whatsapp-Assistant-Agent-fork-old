"""Turning an incoming WhatsApp photo into something the model can look at.

No model runs here any more. The reply is written by Claude, which reads the
picture itself, so the whole job of this file is to check that what arrived
really is an image the Messages API accepts and to shrink it first. What it
used to do — load a ~1 GB BLIP captioner and hand the reply model one English
sentence about the photo — is gone, along with torch and transformers.
"""
# Kept for the same reason app/stt.py keeps it: a module-level annotation is
# evaluated at import time, so the next person to annotate anything here with
# a Pillow type would undo the deferred import below without noticing.
from __future__ import annotations

import base64
import io

from app.log import log_event

# The four the Messages API accepts as an image source, and what each of
# Pillow's format names maps to. Anything else — an HEIC straight off an
# iPhone, a PDF, a sticker — has to be held: the request would be rejected
# outright, and a rejected request is a reply that never arrives.
MEDIA_TYPE_BY_FORMAT = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}
SUPPORTED_MEDIA_TYPES = tuple(MEDIA_TYPE_BY_FORMAT.values())

# Longest edge in pixels, past which the photo is scaled down. The model's own
# ceiling is 2576, and an image that large costs roughly three times the tokens
# of one at 1568 for detail nobody reacting to a friend's photo needs. It is
# also what keeps the request inside the API's size limit: WhatsApp photos
# arrive at several megapixels and base64 inflates them by a third.
MAX_EDGE = 1568

# Ceiling on the base64 the bridge hands us, checked before anything is
# decoded. 16 MiB of base64 is ~12 MiB of image, past anything WhatsApp sends
# as a photo, and decoding it would mean holding that much plus a full decoded
# bitmap on a host that is already swapping. Held instead.
MAX_BASE64_CHARS = 16 * 1024 * 1024


class ImageUnusable(Exception):
    """This photo cannot be sent to the model.

    The message is one short clause written for the owner, because it ends up
    in the Held tab beside her message rather than in a log she never reads.
    """


def image_block(media_base64: str, mimetype: str | None = None) -> dict:
    """The photo as a Messages API image content block, scaled down if it is big.

    `mimetype` is what the bridge read off the WhatsApp message. It is treated
    as a claim and checked against the bytes, because the bytes are what the
    API validates — and because a bridge that sends no mimetype at all should
    still get its photos answered.

    Raises ImageUnusable for anything that cannot be sent, so the caller can
    hold the message instead of guessing a reply at a photo nobody saw.
    """
    # Pillow is 16 MiB of import and only this path ever needs it, which is why
    # it is imported here and not at the top of the file. A test in
    # tests/test_lazy_models.py holds that property.
    from PIL import Image, UnidentifiedImageError

    declared = (mimetype or "").split(";")[0].strip().lower()
    if declared and declared not in SUPPORTED_MEDIA_TYPES:
        raise ImageUnusable(f"the photo is in a format I can't read ({declared[:40]})")
    if len(media_base64) > MAX_BASE64_CHARS:
        raise ImageUnusable("the photo is too big to send to the model")

    try:
        # Not validate=True: the bridge's base64 is machine-written, so a
        # stray newline in it would fail a message that is perfectly fine,
        # and Pillow below is the check that actually matters.
        raw = base64.b64decode(media_base64)
    except ValueError:
        raise ImageUnusable("the photo didn't come through in one piece") from None

    try:
        image = Image.open(io.BytesIO(raw))
        # Open only reads the header, so neither of these decodes the pixels.
        actual_format = (image.format or "").upper()
        width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError):
        raise ImageUnusable("the photo didn't come through in one piece") from None

    media_type = MEDIA_TYPE_BY_FORMAT.get(actual_format)
    if media_type is None:
        raise ImageUnusable(
            f"the photo is in a format I can't read ({actual_format.lower()[:40] or 'unknown'})")

    if max(width, height) <= MAX_EDGE:
        # Small enough already: pass the bytes on exactly as they arrived
        # rather than paying a decode and a re-encode for nothing. An animated
        # GIF keeps its animation this way too.
        return {"type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": media_base64}}

    try:
        image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
        # JPEG at 85 is the cheap end of "you cannot see the difference" on a
        # photo, and the one format everything here can be written to —
        # convert("RGB") because a PNG's alpha or a GIF's palette cannot. An
        # animated GIF this large loses its animation; one frame the model can
        # see beats a picture it cannot.
        shrunk = io.BytesIO()
        image.convert("RGB").save(shrunk, format="JPEG", quality=85)
    except (OSError, ValueError):
        raise ImageUnusable("the photo didn't come through in one piece") from None

    log_event("image_downscaled", before=f"{width}x{height}",
              after=f"{image.width}x{image.height}", kb=len(shrunk.getvalue()) // 1024)
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": base64.b64encode(shrunk.getvalue()).decode()}}
