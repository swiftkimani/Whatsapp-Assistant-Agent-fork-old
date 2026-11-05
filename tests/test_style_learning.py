"""Learning the owner's voice from their own WhatsApp, and ranking it back out.

No network and no bridge: every test drives a fake bridge.post. Where a time
appears it is a fixed clock, so the written files are byte-identical run to run.
"""
import asyncio
import importlib.util
import json
from pathlib import Path

from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import HTTPException

from app import llm, style_learning

# 2026-11-05 12:00:00 UTC, which is 15:00 in Nairobi. A fixed clock: the
# examples file carries the date it was learned, and the backup beside it is
# named after the second it was made.
CLOCK = 1793880000.0

APP_DIR = Path(style_learning.__file__).resolve().parent


@pytest.fixture()
def style_files(tmp_path, monkeypatch):
    """The two files the learning step writes, in a throwaway directory."""
    monkeypatch.setattr(llm, "STYLE_EXAMPLES_PATH", tmp_path / "style_examples.md")
    monkeypatch.setattr(style_learning, "PAIRS_FILE", tmp_path / "style_pairs.json")
    # Fixed as well as the clock: the date written into the file header is the
    # owner's local date, and the host running the tests has its own TZ.
    monkeypatch.setattr(style_learning, "LOCAL_TZ", ZoneInfo("Africa/Nairobi"))
    return tmp_path


def fake_bridge(monkeypatch, chats, threads, calls=None):
    """Stand in for the WhatsApp bridge. `threads` maps chat id -> messages."""
    async def post(path, payload, timeout):
        if calls is not None:
            calls.append((path, payload))
        if path == "/chats":
            body = {"chats": chats[: payload["limit"]]}
        else:
            messages = threads.get(payload["chat_id"], [])
            body = {"chat": {"id": payload["chat_id"]}, "messages": messages[: payload["limit"]]}
        return httpx.Response(200, json=body)

    monkeypatch.setattr(style_learning.bridge, "post", post)


def chat(chat_id, **extra):
    return {"id": chat_id, "name": "", "is_group": False, "left": False, **extra}


def message(body, from_me=False, **extra):
    return {"body": body, "from_me": from_me, "has_media": False, "kind": "", "forwarded": False, **extra}


# ------------------------------------------------------- building the pairs ----

def test_pairs_are_built_from_the_owners_own_replies_not_the_other_persons():
    pairs = style_learning.pairs_from_thread([
        message("niaje"),
        message("poa sana manze", from_me=True),
        message("tuonane kesho"),
        message("eeh fiti", from_me=True),
    ])
    assert pairs == [
        {"incoming": "niaje", "reply": "poa sana manze"},
        {"incoming": "tuonane kesho", "reply": "eeh fiti"},
    ]


def test_an_incoming_message_is_never_learned_as_a_reply():
    pairs = style_learning.pairs_from_thread([message("hii ni mimi peke yangu"), message("na hii pia")])
    assert pairs == []      # nothing the owner sent, so there is no voice here


def test_a_reply_containing_digits_is_dropped():
    pairs = style_learning.pairs_from_thread([
        message("tunaonana saa ngapi"),
        message("saa 4 kwa gate", from_me=True),
        message("sawa"),
        message("tuonane hapo", from_me=True),
    ])
    assert pairs == [{"incoming": "sawa", "reply": "tuonane hapo"}]


def test_media_and_link_messages_are_skipped():
    pairs = style_learning.pairs_from_thread([
        message("", kind="Photo", has_media=True),
        message("umeona hiyo", from_me=True),
        message("https://example.com/story"),
        message("nimeshika", from_me=True),
        message("umefika"),
        message("bado niko njiani", from_me=True),
    ])
    # The photo carries no words of its own and the link is somebody else's
    # page, so neither becomes the message a reply is learned against.
    assert pairs == [{"incoming": "umefika", "reply": "bado niko njiani"}]


def test_a_forwarded_message_is_not_learned_as_the_owners_voice():
    pairs = style_learning.pairs_from_thread([
        message("umeskia"),
        message("Good morning family, remember to pray", from_me=True, forwarded=True),
        message("ok"),
        message("niko hapa", from_me=True),
    ])
    # The forwarded line is gone; the two messages left on their side join into
    # the one thing his reply was answering.
    assert pairs == [{"incoming": "umeskia ok", "reply": "niko hapa"}]


def test_groups_and_left_chats_are_not_walked():
    chats = [chat("254700000001@c.us"), chat("1234@g.us", is_group=True),
             chat("254700000002@c.us", left=True), chat("254700000003@c.us")]
    assert style_learning.own_conversations(chats, 10) == ["254700000001@c.us", "254700000003@c.us"]


def test_examples_are_taken_from_every_conversation_not_just_the_busiest():
    busiest = [{"incoming": "x", "reply": f"loud {n}"} for n in range(5)]
    quiet = [{"incoming": "y", "reply": "quiet one"}]
    picked = style_learning.pick_examples([busiest, quiet], limit=3)
    assert "quiet one" in picked
    assert picked[0] == "loud 4"     # newest first: that is the voice he has now


# ------------------------------------------------------------ the whole walk ----

def test_learning_writes_a_style_file_the_prompt_loader_parses(style_files, monkeypatch):
    fake_bridge(monkeypatch,
                [chat("254700000001@c.us")],
                {"254700000001@c.us": [message("niaje"), message("poa manze, uko aje", from_me=True)]})

    result = asyncio.run(style_learning.learn_style(now=CLOCK))

    assert result == {"pairs": 1, "lines": 1, "chats_read": 1, "backed_up": ""}
    # The one reader that matters: app.llm builds the system prompt with this.
    assert llm._load_style_examples() == "- poa manze, uko aje"
    assert "Learned from your own WhatsApp on 05 Nov 2026, 15:00" in \
        llm.STYLE_EXAMPLES_PATH.read_text(encoding="utf-8")


def test_learning_stores_pairs_for_retrieval_with_the_time_it_ran(style_files, monkeypatch):
    fake_bridge(monkeypatch,
                [chat("254700000001@c.us")],
                {"254700000001@c.us": [message("umefika"), message("bado niko njiani", from_me=True)]})

    asyncio.run(style_learning.learn_style(now=CLOCK))

    stored = json.loads(style_learning.PAIRS_FILE.read_text(encoding="utf-8"))
    assert stored["learned_at"] == CLOCK
    assert stored["pairs"] == [{"incoming": "umefika", "reply": "bado niko njiani"}]
    assert style_learning.style_status() == {
        "is_placeholder": False, "lines": 1, "pairs": 1, "learned_at": CLOCK}


def test_the_shipped_placeholders_are_reported_as_not_filled_in_yet(style_files):
    llm.STYLE_EXAMPLES_PATH.write_text(
        "# How I text\n\nReplace every line below.\n\n- niaje, uko aje\n- poa, nimeshika\n", encoding="utf-8")
    status = style_learning.style_status()
    assert status["is_placeholder"] is True
    assert status["lines"] == 2
    assert status["learned_at"] is None


def test_a_hand_edited_style_file_is_backed_up_rather_than_destroyed(style_files, monkeypatch):
    mine = "# How I text\n\n- mambo vipi mkuu\n- nitakupigia nikifika\n"
    llm.STYLE_EXAMPLES_PATH.write_text(mine, encoding="utf-8")
    fake_bridge(monkeypatch,
                [chat("254700000001@c.us")],
                {"254700000001@c.us": [message("niaje"), message("poa manze", from_me=True)]})

    result = asyncio.run(style_learning.learn_style(now=CLOCK))

    backup = llm.STYLE_EXAMPLES_PATH.with_name(f"style_examples.backup-{int(CLOCK)}.md")
    assert result["backed_up"] == backup.name      # and the owner is told
    assert backup.read_text(encoding="utf-8") == mine
    assert "poa manze" in llm.STYLE_EXAMPLES_PATH.read_text(encoding="utf-8")


def test_placeholders_and_our_own_output_are_replaced_without_a_backup(style_files, monkeypatch):
    llm.STYLE_EXAMPLES_PATH.write_text("# How I text\n\n- niaje, uko aje\n- poa, nimeshika\n", encoding="utf-8")
    fake_bridge(monkeypatch,
                [chat("254700000001@c.us")],
                {"254700000001@c.us": [message("niaje"), message("poa manze", from_me=True)]})

    assert asyncio.run(style_learning.learn_style(now=CLOCK))["backed_up"] == ""
    # Learning twice must not pile up backups of this module's own writing.
    assert asyncio.run(style_learning.learn_style(now=CLOCK))["backed_up"] == ""
    assert list(style_files.glob("*.backup-*")) == []


def test_the_ceilings_bound_how_much_of_the_account_is_read(style_files, monkeypatch):
    chats = [chat(f"2547000000{n:02d}@c.us") for n in range(90)]
    threads = {c["id"]: [message("niaje"), message("poa manze", from_me=True)] * 40 for c in chats}
    calls = []
    fake_bridge(monkeypatch, chats, threads, calls)

    result = asyncio.run(style_learning.learn_style(max_chats=3, max_messages_per_chat=6, now=CLOCK))

    listing, threads_read = calls[0], calls[1:]
    assert listing[0] == "/chats"
    assert listing[1]["limit"] == 3 * style_learning.CHAT_OVERREAD
    assert len(threads_read) == 3                                   # not one call per conversation on the phone
    assert all(payload["limit"] == 6 for _, payload in threads_read)
    assert result["chats_read"] == 3


def test_at_most_the_line_ceiling_reaches_the_prompt(style_files, monkeypatch):
    letters = "abcdefghijklmnopqrst"
    chats = [chat(f"2547000000{n:02d}@c.us") for n in range(len(letters))]
    # Distinct replies everywhere, written without a digit in them so the cap
    # is the only thing that can hold the count down.
    threads = {
        c["id"]: [m for tail in letters[:10]
                  for m in (message(f"swali {letters[i]}{tail}"),
                            message(f"jibu {letters[i]}{tail}", from_me=True))]
        for i, c in enumerate(chats)
    }
    fake_bridge(monkeypatch, chats, threads)

    result = asyncio.run(style_learning.learn_style(now=CLOCK))

    assert result["lines"] == style_learning.MAX_EXAMPLES
    assert len(llm._load_style_examples().splitlines()) == style_learning.MAX_EXAMPLES


def test_an_unreachable_bridge_is_reported_in_words_the_owner_can_act_on(style_files, monkeypatch):
    async def refuse(path, payload, timeout):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(style_learning.bridge, "post", refuse)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(style_learning.learn())

    assert raised.value.status_code == 502
    assert raised.value.detail == style_learning.bridge.UNREACHABLE
    assert not llm.STYLE_EXAMPLES_PATH.exists()     # nothing was overwritten on the way down


def test_an_account_with_nothing_to_learn_from_says_so(style_files, monkeypatch):
    fake_bridge(monkeypatch, [chat("1234@g.us", is_group=True)], {})

    with pytest.raises(HTTPException) as raised:
        asyncio.run(style_learning.learn())

    assert raised.value.status_code == 422
    assert "nothing to learn from" in raised.value.detail


def test_a_conversation_that_cannot_be_read_does_not_cost_the_whole_walk(style_files, monkeypatch):
    async def post(path, payload, timeout):
        if path == "/chats":
            return httpx.Response(200, json={"chats": [chat("254700000001@c.us"), chat("254700000002@c.us")]})
        if payload["chat_id"] == "254700000001@c.us":
            return httpx.Response(404, json={"error": "no such chat"})
        return httpx.Response(200, json={"messages": [message("niaje"), message("poa manze", from_me=True)]})

    monkeypatch.setattr(style_learning.bridge, "post", post)

    result = asyncio.run(style_learning.learn_style(now=CLOCK))

    assert result["chats_read"] == 1
    assert result["pairs"] == 1


# ------------------------------------------------------- lexical retrieval ----

@pytest.fixture()
def retrieval(tmp_path, monkeypatch):
    """The real app.style_retrieval, loaded under a name of its own.

    conftest.py stubs app.style_retrieval for the whole suite so the rest of
    the tests never touch it, so the real module is loaded straight from its
    file here rather than fighting that stub.
    """
    spec = importlib.util.spec_from_file_location("style_retrieval_under_test", APP_DIR / "style_retrieval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PAIRS_FILE", tmp_path / "style_pairs.json")
    monkeypatch.setenv("STYLE_RETRIEVAL", "true")
    return module


def write_pairs(module, pairs):
    module.PAIRS_FILE.write_text(json.dumps({"learned_at": CLOCK, "pairs": pairs}, ensure_ascii=False),
                                 encoding="utf-8")


def test_retrieval_returns_the_pair_whose_message_is_most_like_this_one(retrieval):
    write_pairs(retrieval, [
        {"incoming": "umefika ama bado uko job", "reply": "bado niko job"},
        {"incoming": "unakuja party kesho", "reply": "nitajaribu kufika"},
        {"incoming": "umepata hizo files", "reply": "wacha nicheki"},
    ])
    best = retrieval.retrieve_examples("uko job ama umefika", k=1)
    assert best == [{"incoming": "umefika ama bado uko job", "reply": "bado niko job"}]


def test_retrieval_ranks_the_closest_first_and_honours_k(retrieval):
    write_pairs(retrieval, [
        {"incoming": "unakuja party kesho", "reply": "nitajaribu"},
        {"incoming": "umepata files za party", "reply": "wacha nicheki"},
        {"incoming": "habari ya kazi", "reply": "poa"},
    ])
    found = retrieval.retrieve_examples("unakuja party", k=2)
    assert [p["incoming"] for p in found] == ["unakuja party kesho", "umepata files za party"]


def test_a_message_sharing_no_words_retrieves_nothing(retrieval):
    write_pairs(retrieval, [{"incoming": "umefika", "reply": "bado niko njiani"}])
    assert retrieval.retrieve_examples("completely different wording") == []


def test_retrieval_returns_nothing_when_nobody_has_learned_a_style_yet(retrieval):
    assert not retrieval.PAIRS_FILE.exists()
    assert retrieval.retrieve_examples("umefika") == []


def test_retrieval_survives_a_half_written_pairs_file(retrieval):
    retrieval.PAIRS_FILE.write_text('{"pairs": [{"incoming": "umefi', encoding="utf-8")
    assert retrieval.retrieve_examples("umefika") == []


def test_retrieval_can_be_switched_off(retrieval, monkeypatch):
    write_pairs(retrieval, [{"incoming": "umefika", "reply": "bado niko njiani"}])
    monkeypatch.setenv("STYLE_RETRIEVAL", "false")
    assert retrieval.retrieve_examples("umefika") == []


def test_a_fresh_learn_run_is_picked_up_without_a_restart(retrieval):
    write_pairs(retrieval, [{"incoming": "umefika", "reply": "bado niko njiani"}])
    assert retrieval.retrieve_examples("umefika")
    write_pairs(retrieval, [{"incoming": "umefika", "reply": "niko hapa nje"}])
    assert retrieval.retrieve_examples("umefika") == [{"incoming": "umefika", "reply": "niko hapa nje"}]
