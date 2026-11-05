"""Stickers: sending one from the pane, and reading one back out of a thread.

Half of what gets said on WhatsApp gets said in stickers, and the pane could
neither send one nor show one properly. The validation itself — is this really
a WebP, is it small enough, is it 512x512 — belongs to the bridge, which is the
side that knows what whatsapp-web.js will do with the bytes, and is tested in
whatsapp-bridge/wa_read.test.js (node --test). What is tested here is the way
in and the way out: the password, the bounds, and the bridge's own sentences
reaching the person who has to act on them.
"""
import base64
from types import SimpleNamespace

import httpx
import pytest

from app import dashboard, db

CHAT = "254712345678@c.us"

# A real two-frame animated WebP, 512x512 and 140 bytes: VP8X with WhatsApp's
# own sticker dimensions and the animation flag set. The same file the bridge's
# tests send, so both sides are talking about one picture.
ANIMATED_512 = (
    "UklGRoQAAABXRUJQVlA4WAoAAAACAAAA/wEA/wEAQU5JTQYAAAAAAAAAAABBTk1GKAAAAAAAAAAAAP8"
    "BAP8BAHgAAAJWUDhMDwAAAC//wX8ABxD9j/4HIqL/AQBBTk1GKAAAAAAAAAAAAP8BAP8BAHgAAABWU"
    "DhMDwAAAC//wX8ABxDR//4HIqL/AQA="
)


@pytest.fixture()
def bridge(monkeypatch):
    """Stands in for the bridge: `calls` records what it was asked for, and a
    test can change `replies` to make one route refuse."""
    fake = SimpleNamespace(calls=[], replies={
        "/send-sticker": (200, {"status": "sent", "msg_id": f"true_{CHAT}_3EB0FF11AA22"}),
        "/media": (200, {"mimetype": "image/webp", "data": ANIMATED_512, "filename": ""}),
    })

    async def fake_post(path, payload, timeout):
        fake.calls.append({"path": path, "payload": payload})
        code, body = fake.replies[path]
        return httpx.Response(code, json=body)

    dashboard._last_send.clear()
    monkeypatch.setattr(dashboard, "_bridge_post", fake_post)
    return fake


def sending(**over) -> dict:
    return {"chat_id": CHAT, "mimetype": "image/webp", "data": ANIMATED_512, **over}


# ---- sending one ----

def test_sending_a_sticker_needs_the_password(client, bridge):
    assert client.post("/api/wa/send-sticker", json=sending()).status_code == 401
    assert bridge.calls == []


def test_a_sticker_reaches_the_bridge_whole_and_says_what_it_became(signed_in, bridge):
    r = signed_in.post("/api/wa/send-sticker", json=sending())
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "sent", "msg_id": f"true_{CHAT}_3EB0FF11AA22"}
    # Passed through exactly as it arrived: the bridge is the side that knows
    # what WhatsApp will accept, so nothing is re-encoded on the way.
    assert bridge.calls[0] == {"path": "/send-sticker", "payload": sending()}


def test_a_sticker_is_not_written_into_the_message_log(signed_in, bridge, fresh_db):
    """There is no text to write. "[sticker]" standing in for one would end up
    in what the assistant reads back as examples of how its owner writes."""
    assert signed_in.post("/api/wa/send-sticker", json=sending()).status_code == 200
    assert db.query_messages(None, None, None, "out", 10) == []


def test_a_sticker_is_never_sent_into_a_group(signed_in, bridge):
    r = signed_in.post("/api/wa/send-sticker", json=sending(chat_id="120363000000000001@g.us"))
    assert r.status_code == 400
    assert "group" in r.json()["detail"]
    assert bridge.calls == []


@pytest.mark.parametrize("chat_id", ["", "not-an-id", "254712345678", "status@broadcast",
                                     f"{CHAT}; rm -rf /", "../../etc/passwd"])
def test_a_chat_id_that_is_not_one_never_reaches_the_bridge(signed_in, bridge, chat_id):
    assert signed_in.post("/api/wa/send-sticker",
                          json=sending(chat_id=chat_id)).status_code == 422
    assert bridge.calls == []


@pytest.mark.parametrize("data", ["", "AAAA", base64.b64encode(b"x" * 600_000).decode()])
def test_an_empty_or_oversized_picture_is_refused_before_the_wire(signed_in, bridge, data):
    """The bridge has the real ceiling, in bytes, and a sentence to go with it.
    This only keeps something nobody could render off the wire at all."""
    assert signed_in.post("/api/wa/send-sticker", json=sending(data=data)).status_code == 422
    assert bridge.calls == []


@pytest.mark.parametrize("mimetype", ["", "image", "not a mimetype", "image/" + "x" * 80])
def test_a_mimetype_that_is_not_one_is_refused_here(signed_in, bridge, mimetype):
    assert signed_in.post("/api/wa/send-sticker",
                          json=sending(mimetype=mimetype)).status_code == 422
    assert bridge.calls == []


def test_a_mimetype_that_is_one_is_the_bridge_s_to_judge(signed_in, bridge):
    """A GIF is a real mimetype and a bad sticker, and the bridge is the side
    that can say why in a sentence worth reading. Refusing it here as a field
    error would leave the owner trying it again with another GIF."""
    bridge.replies["/send-sticker"] = (400, {
        "error": "an animated sticker has to be an animated WebP file."
                 " This cannot turn a video or a GIF into one."})
    r = signed_in.post("/api/wa/send-sticker", json=sending(mimetype="image/gif"))
    assert r.status_code == 400
    assert "animated WebP" in r.json()["detail"]
    assert bridge.calls[0]["payload"]["mimetype"] == "image/gif"


@pytest.mark.parametrize("code,error", [
    (400, "a sticker is 512x512 or smaller, and that one is 1024x1024."),
    (400, "that file is not really a WEBP"),
    (413, "a sticker has to be smaller than 500 KB"),
    (429, "this account has sent as much as it safely should in a minute."
          " Try again a little later."),
])
def test_the_bridge_s_refusal_reaches_the_owner_in_its_own_words(signed_in, bridge, code, error):
    """Every one of these is a sentence somebody can act on — send it as a
    PNG, pick a smaller one, wait. Flattened into "the bridge could not do
    that" they are all the same dead end."""
    bridge.replies["/send-sticker"] = (code, {"error": error})
    r = signed_in.post("/api/wa/send-sticker", json=sending())
    assert r.status_code == code
    assert r.json()["detail"] == error


def test_a_conversation_that_is_gone_says_so_rather_than_no_such_chat(signed_in, bridge):
    """The bridge's own 404 is the one answer it gives that is not a sentence."""
    bridge.replies["/send-sticker"] = (404, {"error": "no such chat"})
    r = signed_in.post("/api/wa/send-sticker", json=sending())
    assert r.status_code == 404
    assert "not on WhatsApp" in r.json()["detail"]


def test_a_bridge_that_fails_oddly_is_not_reported_as_the_owner_s_mistake(signed_in, bridge):
    bridge.replies["/send-sticker"] = (500, {"error": "request failed"})
    assert signed_in.post("/api/wa/send-sticker", json=sending()).status_code == 502


def test_two_stickers_in_the_same_instant_are_refused(signed_in, bridge):
    """Stickers are sent by tapping, and a burst of them is what an automated
    account looks like from WhatsApp's side."""
    assert signed_in.post("/api/wa/send-sticker", json=sending()).status_code == 200
    r = signed_in.post("/api/wa/send-sticker", json=sending())
    assert r.status_code == 429
    assert "wait" in r.json()["detail"].lower()


def test_a_refused_sticker_does_not_start_the_cooldown(signed_in, bridge):
    bridge.replies["/send-sticker"] = (400, {"error": "that file is not really a WEBP"})
    assert signed_in.post("/api/wa/send-sticker", json=sending()).status_code == 400
    assert "sticker" not in dashboard._last_send


# ---- reading one back ----

def test_an_animated_sticker_keeps_its_webp_type_through_the_round_trip(signed_in, bridge):
    """An animated sticker is an animated WebP. Served as anything else — or
    re-encoded on the way — it arrives as a still, or as a broken image, and
    the thread shows a grey box where the joke was.
    """
    r = signed_in.get("/api/wa/media", params={"chat_id": CHAT, "msg_id": "A"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/webp")
    # Shown in place rather than offered as a download.
    assert "inline" in r.headers["content-disposition"]
    # Byte for byte what the bridge handed over, animation chunk and all.
    assert r.content == base64.b64decode(ANIMATED_512)
    assert r.content.startswith(b"RIFF") and r.content[8:12] == b"WEBP"
    assert b"ANIM" in r.content[:40], "an animated sticker that lost its frames is a still"
