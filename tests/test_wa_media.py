"""Serving one attachment, and a profile photo, out of the owner's WhatsApp.

Both go through the bridge, and both are deliberately fetched one at a time:
every one is a download through the bridge's own browser session.
"""
import base64
from types import SimpleNamespace

import httpx
import pytest

from app import dashboard

PIXEL = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()
CHAT = "254712345678@c.us"


@pytest.fixture()
def bridge(monkeypatch):
    fake = SimpleNamespace(calls=[], replies={
        "/media": (200, {"mimetype": "image/png", "data": PIXEL, "filename": "photo.png"}),
        "/avatar": (200, {"url": "https://pps.whatsapp.net/v/photo.jpg"}),
    })

    async def fake_post(path, payload, timeout):
        fake.calls.append({"path": path, "payload": payload})
        code, body = fake.replies[path]
        return httpx.Response(code, json=body)

    monkeypatch.setattr(dashboard, "_bridge_post", fake_post)
    return fake


@pytest.fixture()
def cdn(monkeypatch):
    """Stands in for WhatsApp's own image host, which the backend fetches so
    the browser never talks to it."""
    state = SimpleNamespace(urls=[], response=httpx.Response(200, content=b"JPEGBYTES",
                                                             headers={"content-type": "image/jpeg"}))

    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            state.urls.append(url)
            if isinstance(state.response, Exception):
                raise state.response
            return state.response

    monkeypatch.setattr(dashboard.httpx, "AsyncClient", FakeClient)
    return state


def media_url(msg_id="A", chat=CHAT):
    return f"/api/wa/media?chat_id={chat}&msg_id={msg_id}"


def test_an_attachment_comes_back_as_its_own_bytes(signed_in, bridge):
    r = signed_in.get(media_url())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content.startswith(b"\x89PNG")
    assert bridge.calls[0]["payload"] == {"chat_id": CHAT, "msg_id": "A"}


def test_a_renderable_type_is_shown_in_place_and_anything_else_is_a_download(signed_in, bridge):
    assert "inline" in signed_in.get(media_url()).headers["content-disposition"]
    bridge.replies["/media"] = (200, {"mimetype": "application/pdf", "data": PIXEL})
    r = signed_in.get(media_url())
    assert "attachment" in r.headers["content-disposition"]
    # A file the page would not render is never handed over as something it might.
    assert r.headers["content-type"].startswith("application/octet-stream")


def test_an_attachment_whatsapp_has_dropped_says_so(signed_in, bridge):
    bridge.replies["/media"] = (404, {"error": "media is gone"})
    r = signed_in.get(media_url())
    assert r.status_code == 404 and "no longer on WhatsApp" in r.json()["detail"]


def test_something_too_large_is_refused_rather_than_held_in_memory(signed_in, bridge):
    bridge.replies["/media"] = (413, {"error": "too large"})
    assert signed_in.get(media_url()).status_code == 413


def test_damaged_bytes_do_not_reach_the_page(signed_in, bridge):
    bridge.replies["/media"] = (200, {"mimetype": "image/png", "data": "not base64!!"})
    assert signed_in.get(media_url()).status_code == 502


def test_a_bad_chat_id_never_reaches_the_bridge(signed_in, bridge):
    assert signed_in.get(media_url(chat="not-a-chat")).status_code == 422
    assert bridge.calls == []


def test_an_attachment_needs_a_session(client, bridge):
    assert client.get(media_url()).status_code == 401


def test_a_profile_photo_is_fetched_here_not_by_the_browser(signed_in, bridge, cdn):
    r = signed_in.get(f"/api/wa/avatar?chat_id={CHAT}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/jpeg")
    assert r.content == b"JPEGBYTES"
    # WhatsApp's own URL is used here and never handed to the page.
    assert cdn.urls == ["https://pps.whatsapp.net/v/photo.jpg"]
    assert "pps.whatsapp.net" not in r.text


def test_no_photo_is_an_ordinary_answer(signed_in, bridge, cdn):
    """The other person may simply have their privacy set so you cannot see one."""
    bridge.replies["/avatar"] = (404, {"error": "no photo"})
    assert signed_in.get(f"/api/wa/avatar?chat_id={CHAT}").status_code == 404


def test_a_photo_url_that_is_not_https_is_refused(signed_in, bridge, cdn):
    bridge.replies["/avatar"] = (200, {"url": "http://pps.whatsapp.net/v/photo.jpg"})
    assert signed_in.get(f"/api/wa/avatar?chat_id={CHAT}").status_code == 404
    assert cdn.urls == []


def test_something_that_is_not_an_image_is_not_served_as_one(signed_in, bridge, cdn):
    cdn.response = httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
    assert signed_in.get(f"/api/wa/avatar?chat_id={CHAT}").status_code == 404


def test_a_photo_host_that_will_not_answer_is_not_a_crash(signed_in, bridge, cdn):
    cdn.response = httpx.ConnectError("down")
    assert signed_in.get(f"/api/wa/avatar?chat_id={CHAT}").status_code == 502


# ---- linking: the one thing that has to work when nothing else does ----

@pytest.fixture()
def link_bridge(monkeypatch):
    state = SimpleNamespace(reply=(200, {"linked": False, "qr_svg": "<svg>code</svg>"}))

    async def fake_post(path, payload, timeout):
        assert path == "/link-status"
        if isinstance(state.reply, Exception):
            raise state.reply
        code, body = state.reply
        return httpx.Response(code, json=body)

    monkeypatch.setattr(dashboard.bridge, "post", fake_post)
    return state


def test_an_unlinked_bridge_hands_back_a_code_to_scan(signed_in, link_bridge):
    r = signed_in.get("/api/wa/link").json()
    assert r["reachable"] is True and r["linked"] is False
    assert r["qr_svg"] == "<svg>code</svg>"


def test_a_refused_scan_says_why_instead_of_looking_like_no_scan(signed_in, link_bridge):
    """Without this the two are indistinguishable: WhatsApp rejecting the link
    and nobody having scanned both leave an unlinked bridge cycling codes."""
    link_bridge.reply = (200, {"linked": False, "qr_svg": None,
                               "stage": "refused", "error": "Unable to log in"})
    r = signed_in.get("/api/wa/link").json()
    assert r["error"] == "Unable to log in" and r["stage"] == "refused"


def test_a_linked_bridge_says_so_and_offers_no_code(signed_in, link_bridge):
    link_bridge.reply = (200, {"linked": True, "qr_svg": None})
    r = signed_in.get("/api/wa/link").json()
    assert r["linked"] is True and r["qr_svg"] is None


def test_a_bridge_that_is_down_is_not_the_same_as_one_that_is_unlinked(signed_in, link_bridge):
    """One needs the process started; the other needs a phone. Telling the
    owner the wrong one sends them to the wrong place."""
    link_bridge.reply = httpx.ConnectError("down")
    assert signed_in.get("/api/wa/link").json()["reachable"] is False


def test_a_bridge_that_errors_is_reported_as_unlinked_rather_than_crashing(signed_in, link_bridge):
    link_bridge.reply = (500, {"error": "boom"})
    r = signed_in.get("/api/wa/link").json()
    assert r["reachable"] is True and r["linked"] is False


def test_the_link_status_needs_a_session(client, link_bridge):
    assert client.get("/api/wa/link").status_code == 401
