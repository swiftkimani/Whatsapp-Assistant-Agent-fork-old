"""The dashboard's WhatsApp pane: reading the owner's own threads and answering
them. The shaping of chats and messages happens in whatsapp-bridge/wa_read.js
and is tested there (npm test)."""
import time
from types import SimpleNamespace

import httpx
import pytest

from app import dashboard, db, live

CHATS = {"chats": [
    {"id": "254712345678@c.us", "name": "Jane", "is_group": False,
     "unread": 2, "ts": 1762000000, "preview": "niaje", "from_me": False},
]}
THREAD = {
    "chat": {"id": "254712345678@c.us", "name": "Jane", "is_group": False},
    "messages": [{"id": "A", "ts": 1762000000, "from_me": False, "author": "",
                  "body": "niaje", "kind": ""}],
}


@pytest.fixture()
def bridge(monkeypatch):
    """Stands in for the bridge: `calls` records what it was asked for, and a
    test can change `replies` to make one route fail."""
    fake = SimpleNamespace(
        calls=[],
        replies={"/chats": (200, CHATS), "/thread": (200, THREAD), "/send-reply": (200, {"status": "sent"})},
    )

    async def fake_post(path, payload, timeout):
        fake.calls.append({"path": path, "payload": payload})
        code, body = fake.replies[path]
        return httpx.Response(code, json=body)

    dashboard._last_send.clear()
    monkeypatch.setattr(dashboard, "_bridge_post", fake_post)
    return fake


PROTECTED = [("get", "/api/wa/chats"), ("get", "/api/wa/thread?chat_id=1@c.us"),
             ("post", "/api/wa/reply"), ("post", "/api/wa/send-sticker"),
             ("get", "/api/wa/stream")]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_reading_your_whatsapp_needs_the_password(client, method, path):
    assert getattr(client, method)(path).status_code == 401


def test_chats_come_back_as_the_bridge_shaped_them(signed_in, bridge):
    r = signed_in.get("/api/wa/chats")
    assert r.status_code == 200
    got = r.json()["chats"][0]
    # Everything the bridge said survives untouched; the assistant state is
    # added beside it rather than folded into it.
    assert {k: got[k] for k in CHATS["chats"][0]} == CHATS["chats"][0]
    assert bridge.calls[0] == {"path": "/chats", "payload": {"limit": 25}}


def test_each_chat_says_what_the_assistant_will_do_with_it(signed_in, bridge):
    got = signed_in.get("/api/wa/chats").json()
    assert got["default_mode"] == "ignore"
    # Nobody on the contact list, so this chat is taking the default and has
    # not been chosen — a distinction the pane has to be able to draw.
    assert got["chats"][0]["assistant"] == {
        "name": None, "mode": "ignore", "auto": False, "chosen": False, "can_choose": True}


def test_a_thread_is_asked_for_by_chat_id(signed_in, bridge):
    r = signed_in.get("/api/wa/thread", params={"chat_id": "254712345678@c.us", "limit": 10})
    assert r.status_code == 200
    # Everything the bridge shaped, plus who sent it. The field is on every
    # message, including incoming ones, because the page reads it on each.
    assert r.json()["messages"] == [{**m, "sent_by": ""} for m in THREAD["messages"]]
    assert bridge.calls[0]["payload"] == {"chat_id": "254712345678@c.us", "limit": 10}


def test_a_reply_the_assistant_sent_is_named_as_hers(signed_in, bridge):
    """WhatsApp reports an outgoing message and nothing more, so without this
    a reply she sent by herself reads exactly like one typed on the phone.

    The log row is stamped as it is written, so the thread is dated from the
    same clock — a fixed fixture timestamp would sit a year from "now" and
    fall outside the matching window, which is the correct behaviour but not
    what this test is for."""
    chat_id = "254712345678@c.us"
    now = time.time()
    bridge.replies["/thread"] = (200, {
        "chat": THREAD["chat"],
        "messages": [
            {"id": "A", "ts": now - 60, "from_me": False, "author": "", "body": "niaje", "kind": ""},
            {"id": "B", "ts": now - 20, "from_me": True, "author": "", "body": "Niko poa, na wewe?", "kind": ""},
            {"id": "C", "ts": now, "from_me": True, "author": "", "body": "Typed on the phone", "kind": ""},
        ],
    })
    db.log_message(chat_id, "Jane", "out", "Niko poa, na wewe?", "auto")
    messages = signed_in.get("/api/wa/thread", params={"chat_id": chat_id}).json()["messages"]
    assert [m["sent_by"] for m in messages] == ["", "auto", ""]


def test_a_sticker_and_an_emoji_message_reach_the_page_as_the_bridge_shaped_them(signed_in, bridge):
    """The pane adds `sent_by` to every message and passes the rest straight
    through. Both of these carry a field the page needs and nothing else
    carries: a sticker has no words, so `render_as` is the only thing that
    turns it into a picture rather than the word "Sticker"; and `emoji_only`
    is what lets a one-emoji reply be drawn large, the way the phone draws it.
    """
    bridge.replies["/thread"] = (200, {"chat": THREAD["chat"], "messages": [
        {"id": "S", "ts": 1762000001, "from_me": False, "author": "", "body": "",
         "kind": "Sticker", "has_media": True, "media_type": "sticker",
         "render_as": "sticker", "emoji_only": False},
        {"id": "E", "ts": 1762000002, "from_me": True, "author": "", "body": "\U0001f44d",
         "kind": "", "has_media": False, "emoji_only": True},
    ]})
    messages = signed_in.get("/api/wa/thread", params={"chat_id": "254712345678@c.us"}).json()["messages"]
    assert [m["render_as"] for m in messages[:1]] == ["sticker"]
    assert [m["emoji_only"] for m in messages] == [False, True]


@pytest.mark.parametrize("chat_id", ["", "not-an-id", "254712345678", "254712345678@c.us; rm -rf /",
                                     "status@broadcast", "../../etc/passwd"])
def test_a_chat_id_that_is_not_one_is_refused_before_the_bridge(signed_in, bridge, chat_id):
    assert signed_in.get("/api/wa/thread", params={"chat_id": chat_id}).status_code == 422
    assert bridge.calls == []


def test_a_group_can_be_read_but_not_answered(signed_in, bridge):
    assert signed_in.get("/api/wa/thread", params={"chat_id": "254700000000@g.us"}).status_code == 200
    r = signed_in.post("/api/wa/reply", json={"chat_id": "254700000000@g.us", "text": "hi"})
    assert r.status_code == 422                      # the pattern excludes groups
    assert [c["path"] for c in bridge.calls] == ["/thread"]


def test_a_reply_is_sent_and_recorded_in_the_message_log(signed_in, bridge, fresh_db):
    r = signed_in.post("/api/wa/reply", json={"chat_id": "254712345678@c.us", "text": "niaje, nakuja"})
    assert r.status_code == 200 and r.json() == {"status": "sent"}
    assert bridge.calls[0]["payload"] == {
        "chat_id": "254712345678@c.us", "text": "niaje, nakuja", "quoted_msg_id": ""}
    logged = db.query_messages(None, None, None, "out", 10)
    assert [(m["direction"], m["body"]) for m in logged] == [("out", "niaje, nakuja")]


def test_a_conversation_that_no_longer_exists_says_so_rather_than_failing_oddly(signed_in, bridge):
    bridge.replies["/send-reply"] = (404, {"error": "no such chat"})
    r = signed_in.post("/api/wa/reply", json={"chat_id": "254712345678@c.us", "text": "hi"})
    assert r.status_code == 404 and "not on WhatsApp" in r.json()["detail"]
    assert "reply" not in dashboard._last_send      # a failure does not start the cooldown


def test_two_replies_in_the_same_instant_are_refused(signed_in, bridge):
    assert signed_in.post("/api/wa/reply", json={"chat_id": "254712345678@c.us", "text": "a"}).status_code == 200
    assert signed_in.post("/api/wa/reply", json={"chat_id": "254712345678@c.us", "text": "b"}).status_code == 429


def test_an_empty_or_oversized_reply_never_reaches_the_bridge(signed_in, bridge):
    for text in ["", " ".join([]), "x" * 4001]:
        assert signed_in.post("/api/wa/reply", json={"chat_id": "254712345678@c.us", "text": text}).status_code == 422
    assert bridge.calls == []


def test_a_bridge_that_cannot_read_whatsapp_is_reported_as_such(signed_in, bridge):
    bridge.replies["/chats"] = (500, {"error": "request failed"})
    r = signed_in.get("/api/wa/chats")
    assert r.status_code == 502 and "could not read" in r.json()["detail"]


# ---- the live stream: the bridge says a conversation moved, dashboards hear ----

def test_only_the_bridge_can_say_a_conversation_moved(client):
    r = client.post("/wa-event", json={"chat_id": "254712345678@c.us"})
    assert r.status_code == 401
    r = client.post("/wa-event", json={"chat_id": "254712345678@c.us"},
                    headers={"x-bridge-token": "wrong"})
    assert r.status_code == 401


def test_an_activity_notice_reaches_every_open_dashboard(client):
    live._subscribers.clear()
    queue = live.subscribe()
    try:
        r = client.post("/wa-event", json={"chat_id": "254712345678@c.us"},
                        headers={"x-bridge-token": "bridge-secret"})
        assert r.status_code == 200
        assert queue.get_nowait() == ("activity", "254712345678@c.us")
    finally:
        live.unsubscribe(queue)


def test_a_notice_about_something_that_is_not_a_chat_is_refused(client):
    live._subscribers.clear()
    queue = live.subscribe()
    try:
        for bad in ["", "status@broadcast", "../etc", "254712345678"]:
            r = client.post("/wa-event", json={"chat_id": bad},
                            headers={"x-bridge-token": "bridge-secret"})
            assert r.status_code == 422, bad
        assert queue.empty()
    finally:
        live.unsubscribe(queue)


def test_a_signed_out_browser_cannot_open_the_stream_and_leaves_nothing_behind(client):
    live._subscribers.clear()
    assert client.get("/api/wa/stream").status_code == 401
    assert live.listeners() == 0


# ---- choosing, from the conversation itself, who the assistant answers ----

def test_choosing_a_chat_puts_that_person_on_the_list_and_sets_them_to_reply(signed_in, bridge):
    """The point of doing it here: someone who has written to you but was never
    saved on your phone has no name for a setting to hang on, and this is what
    gives them one."""
    r = signed_in.post("/api/wa/assistant",
                       json={"chat_id": "254799000111@c.us", "name": "Kawira", "enabled": True})
    assert r.status_code == 200
    assert r.json()["name"] == "Kawira"
    assert r.json()["assistant"]["auto"] is True
    assert signed_in.get("/api/contacts").json()["contacts"].count("Kawira") == 1


def test_choosing_the_same_chat_twice_does_not_add_them_twice(signed_in, bridge):
    body = {"chat_id": "254799000111@c.us", "name": "Kawira", "enabled": True}
    signed_in.post("/api/wa/assistant", json=body)
    signed_in.post("/api/wa/assistant", json=body)
    assert signed_in.get("/api/contacts").json()["contacts"].count("Kawira") == 1


def test_a_chosen_chat_stays_chosen_when_the_default_moves(signed_in, bridge, fresh_db):
    """What "select the chats I want answered" has to mean: the choice is
    recorded, not inferred from whatever the default happens to be today."""
    fresh_db.set_default_mode("auto")
    signed_in.post("/api/wa/assistant",
                   json={"chat_id": "254799000111@c.us", "name": "Kawira", "enabled": True})
    fresh_db.set_default_mode("ignore")
    assert fresh_db.get_mode("Kawira") == "auto"


def test_turning_it_off_holds_the_reply_rather_than_dropping_the_message(signed_in, bridge, fresh_db):
    signed_in.post("/api/wa/assistant",
                   json={"chat_id": "254799000111@c.us", "name": "Kawira", "enabled": False})
    assert fresh_db.get_mode("Kawira") == "hold"


def test_a_group_cannot_be_chosen_because_it_is_never_answered(signed_in, bridge):
    r = signed_in.post("/api/wa/assistant",
                       json={"chat_id": "254799000111@g.us", "name": "Team", "enabled": True})
    assert r.status_code == 400 and "group" in r.json()["detail"]


def test_choosing_needs_a_session(client, bridge):
    r = client.post("/api/wa/assistant", json={"chat_id": "254799000111@c.us", "enabled": True})
    assert r.status_code == 401


def test_a_classic_group_can_report_that_it_moved(client):
    """The pane learns a conversation changed from this ping. It rejected
    classic group ids and multi-device suffixes, so those chats never moved up
    the list until the slow fallback poll came round."""
    for chat_id in ["254776599915-1497087597@g.us", "254712345678:12@c.us",
                    "120363111122223333@g.us", "254712345678@c.us"]:
        r = client.post("/wa-event", json={"chat_id": chat_id}, headers={"x-bridge-token": "bridge-secret"})
        assert r.status_code == 200, chat_id


def test_something_that_is_not_a_conversation_still_cannot_ping(client):
    for chat_id in ["status@broadcast", "abc@newsletter", "", "../../etc/passwd"]:
        assert client.post("/wa-event", json={"chat_id": chat_id}, headers={"x-bridge-token": "bridge-secret"}).status_code == 422


def test_a_reply_can_quote_a_message_in_the_same_conversation(signed_in, bridge, fresh_db):
    quoted = "false_254712345678@c.us_3EB0AA11BB22"
    r = signed_in.post("/api/wa/reply", json={
        "chat_id": "254712345678@c.us", "text": "saa nne", "quoted_msg_id": quoted})
    assert r.status_code == 200
    assert bridge.calls[0]["payload"]["quoted_msg_id"] == quoted


@pytest.mark.parametrize("quoted", [
    "not-an-id",
    "254712345678@c.us",                       # a chat id is not a message id
    "maybe_254712345678@c.us_3EB0AA11BB22",    # only true_/false_ exist
    "false_254712345678@c.us_3EB0AA11BB22; rm -rf /",
    "false_254712345678@c.us_\n3EB0AA11BB22",
])
def test_a_quoted_id_that_is_not_one_is_refused_before_the_bridge(signed_in, bridge, quoted):
    """A quote carries the quoted text into the conversation, so an id that
    came from anywhere but a real message must not reach the page."""
    r = signed_in.post("/api/wa/reply", json={
        "chat_id": "254712345678@c.us", "text": "hi", "quoted_msg_id": quoted})
    assert r.status_code == 422
    assert bridge.calls == []


def test_a_quote_that_cannot_be_found_says_so_in_its_own_words(signed_in, bridge, fresh_db):
    """The bridge answers 404 for two different missing things, and sending
    the owner to look for the wrong one wastes their time."""
    bridge.replies["/send-reply"] = (404, {"error": "quoted message not found"})
    r = signed_in.post("/api/wa/reply", json={
        "chat_id": "254712345678@c.us", "text": "hi",
        "quoted_msg_id": "false_254712345678@c.us_3EB0AA11BB22"})
    assert r.status_code == 404
    assert "quoted" in r.json()["detail"].lower()


def test_a_chat_can_be_set_to_draft_and_wait_rather_than_only_on_or_off(signed_in, bridge, fresh_db):
    """On or off was never the whole question: on meant either answering by
    herself or writing the reply and waiting, and those are different things
    to agree to for one person."""
    for mode in ("auto", "hold", "ignore"):
        r = signed_in.post("/api/wa/assistant", json={
            "chat_id": "254712345678@c.us", "name": "Jane", "mode": mode})
        assert r.status_code == 200, r.text
        assert r.json()["assistant"]["mode"] == mode


def test_the_older_on_off_shape_still_works(signed_in, bridge, fresh_db):
    """A page cached from before the three-way choice must not start failing."""
    r = signed_in.post("/api/wa/assistant", json={
        "chat_id": "254712345678@c.us", "name": "Jane", "enabled": True})
    assert r.status_code == 200
    assert r.json()["assistant"]["mode"] == "auto"


def test_a_request_that_says_nothing_is_refused(signed_in, bridge, fresh_db):
    """Quietly picking a setting for somebody is worse than refusing."""
    r = signed_in.post("/api/wa/assistant", json={
        "chat_id": "254712345678@c.us", "name": "Jane"})
    assert r.status_code == 400


def test_a_mode_that_is_not_one_of_the_three_is_refused(signed_in, bridge, fresh_db):
    r = signed_in.post("/api/wa/assistant", json={
        "chat_id": "254712345678@c.us", "name": "Jane", "mode": "send_everything"})
    assert r.status_code == 422
