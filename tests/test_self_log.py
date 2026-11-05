"""Telling the owner, on their own phone, which replies were not theirs.

A marker inside the message cannot do this: WhatsApp renders the same bytes
at both ends, so anything the owner could see the recipient could see too.
A copy in the owner's own chat leaves the recipient's message untouched.
"""
import httpx
import pytest

from app import main

HEADERS = {"x-bridge-token": "bridge-secret"}


def msg(**over):
    return {"from": "201000000000@c.us", "number": "201000000000", "body": "hi", "type": "text", **over}


@pytest.fixture()
def sending(monkeypatch, fresh_db):
    """An agent that always sends, and a bridge that records what it was given."""
    sent = []

    def fake_agent(contact_id, message_type, raw_text, media_base64, mimetype=None, direction=""):
        return {"resolved_text": raw_text, "reply_text": "niko njiani", "action": "send", "hold_reason": ""}

    async def fake_post(path, payload, timeout):
        sent.append({"path": path, "payload": payload})
        return httpx.Response(200, json={"status": "sent"})

    monkeypatch.setattr(main, "run_agent", fake_agent)
    monkeypatch.setattr(main.bridge, "post", fake_post)
    fresh_db.set_default_mode("auto")
    return sent


def test_off_by_default_so_nothing_extra_is_sent(client, sending, contacts_file):
    assert client.post("/webhook", json=msg(), headers=HEADERS).json()["action"] == "send"
    assert sending == []


def test_when_on_a_copy_goes_to_the_owners_own_chat(signed_in, sending, fresh_db, contacts_file):
    signed_in.post("/api/self-log", json={"enabled": True})
    assert signed_in.post("/webhook", json=msg(), headers=HEADERS).json()["action"] == "send"

    assert len(sending) == 1 and sending[0]["path"] == "/send-self"
    note = sending[0]["payload"]["text"]
    assert "niko njiani" in note        # what went out
    assert "Mom" in note                # and who it went to


def test_the_recipients_message_is_untouched(signed_in, sending, fresh_db, contacts_file):
    """The reply itself must carry no marker — that is the whole point."""
    signed_in.post("/api/self-log", json={"enabled": True})
    body = signed_in.post("/webhook", json=msg(), headers=HEADERS).json()
    assert body["reply_text"] == "niko njiani"


def test_a_failed_copy_does_not_fail_the_reply(signed_in, sending, fresh_db, contacts_file, monkeypatch):
    signed_in.post("/api/self-log", json={"enabled": True})

    async def unreachable(path, payload, timeout):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(main.bridge, "post", unreachable)
    assert signed_in.post("/webhook", json=msg(), headers=HEADERS).json()["action"] == "send"


def test_a_held_reply_is_not_copied_because_you_already_saw_it(signed_in, sending, fresh_db, contacts_file, monkeypatch):
    signed_in.post("/api/self-log", json={"enabled": True})
    signed_in.post("/api/auto-reply", json={"enabled": False})
    assert signed_in.post("/webhook", json=msg(), headers=HEADERS).json()["action"] == "hold"
    assert sending == []


def test_the_switch_shows_in_status(signed_in):
    assert signed_in.get("/api/status").json()["self_log"] is False
    assert signed_in.post("/api/self-log", json={"enabled": True}).json()["self_log"] is True
    assert signed_in.get("/api/status").json()["self_log"] is True
    assert signed_in.post("/api/self-log", json={"enabled": False}).json()["self_log"] is False
