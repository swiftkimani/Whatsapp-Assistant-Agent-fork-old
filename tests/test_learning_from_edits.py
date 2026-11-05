"""What the assistant learns when the owner rewrites one of her drafts.

send_held() held both the draft she wrote and the words he actually sent, and
threw the difference away. These cover keeping it: which edits are worth
keeping, that the file they go into stays readable however they arrive, that
the cap holds, that the retrieval can really find one, and above all that none
of it can cost him the reply that has already gone out.

No network and no bridge: httpx.post is faked everywhere a send happens, and
every path written is under tmp_path.
"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from app import approvals, llm, style_learning

MOM = "201000000000@c.us"            # in the test contacts.json

APP_DIR = Path(style_learning.__file__).resolve().parent

# 2026-11-05 12:00:00 UTC. Only learn_style writes a time, so this is here for
# the one test that runs the whole walk.
CLOCK = 1793880000.0


@pytest.fixture()
def pairs_file(tmp_path, monkeypatch):
    """The file app.style_retrieval reads, in a throwaway directory."""
    path = tmp_path / "style_pairs.json"
    monkeypatch.setattr(style_learning, "PAIRS_FILE", path)
    return path


@pytest.fixture()
def bridge(monkeypatch):
    """Stands in for the whatsapp-web.js bridge and keeps what it was asked to send."""
    sent = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return httpx.Response(200, json={"status": "sent"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(approvals.httpx, "post", post)
    return sent


def stored(pairs_file):
    """Every pair in the file, which must always be readable JSON."""
    return json.loads(pairs_file.read_text(encoding="utf-8"))["pairs"]


def corrections(pairs_file):
    return [p for p in stored(pairs_file) if p.get("source") == style_learning.CORRECTION]


def write_pairs(pairs_file, pairs, learned_at=CLOCK):
    pairs_file.write_text(json.dumps({"learned_at": learned_at, "pairs": pairs}, ensure_ascii=False),
                          encoding="utf-8")


# ------------------------------------------- what counts as an edit to keep ----

def test_a_rewritten_draft_is_kept_as_the_owners_own_pair(pairs_file):
    assert style_learning.learn_from_edit(
        "umefika ama bado uko job", "I am still at work.", "bado niko job manze") is True

    assert stored(pairs_file) == [{
        "incoming": "umefika ama bado uko job",
        "reply": "bado niko job manze",
        "source": "correction",
    }]


def test_sending_the_draft_as_written_teaches_nothing(pairs_file):
    """Approving what she wrote is not a correction — there is no word of his in it."""
    assert style_learning.learn_from_edit("umefika", "bado niko njiani", "bado niko njiani") is False
    assert not pairs_file.exists()


def test_respacing_or_recapitalising_a_draft_is_not_an_edit(pairs_file):
    assert style_learning.learn_from_edit("umefika", "Bado niko njiani", "bado  niko njiani") is False
    assert not pairs_file.exists()


def test_a_one_character_fix_is_noise_and_is_not_kept(pairs_file):
    """A typo corrected leaves the words hers, so there is no choice of his to learn."""
    assert style_learning.learn_from_edit(
        "umefika ama bado uko njiani", "bado niko njianii kiasi", "bado niko njiani kiasi") is False
    assert not pairs_file.exists()


def test_a_complete_rewrite_is_kept_however_short(pairs_file):
    """The most valuable case: she wrote a paragraph and he answered in two words."""
    draft = ("Hello! I am currently on my way to the meeting and should be arriving "
             "shortly. Thank you for your patience.")
    assert style_learning.learn_from_edit("uko wapi", draft, "niko njiani") is True
    assert corrections(pairs_file)[0]["reply"] == "niko njiani"


def test_an_edit_containing_digits_is_dropped(pairs_file):
    """build_pairs' own rule, applied to his corrections too: a time or a price
    copied back out as fact goes stale, and he would never hear about it."""
    assert style_learning.learn_from_edit(
        "tunaonana saa ngapi", "I will be there soon.", "nitafika saa 4 kwa gate") is False
    assert not pairs_file.exists()


def test_an_edit_that_fails_the_existing_filters_is_dropped_the_same_way(pairs_file):
    """Links, media placeholders and replies too long to be a texting example
    are the export parser's judgement calls, and corrections go through them."""
    assert style_learning.learn_from_edit("umeona hii", "nice", "soma hii https://example.com/story") is False
    assert style_learning.learn_from_edit("umeona hii", "nice", "<Media omitted>") is False
    assert style_learning.learn_from_edit("umeona hii", "nice", "manze " * 40) is False
    assert style_learning.learn_from_edit("", "nice", "poa sana manze") is False     # nothing to pair it with
    assert not pairs_file.exists()


# ------------------------------------------------ through the send endpoint ----

def test_approving_an_edited_draft_stores_it_and_still_reports_the_send(
        signed_in, fresh_db, bridge, pairs_file):
    item = fresh_db.add_held(MOM, "umefika ama bado uko job", "I am still at work.")

    r = signed_in.post(f"/api/held/{item}/send", json={"text": "bado niko job manze"})

    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert [j["text"] for j in bridge] == ["bado niko job manze"]
    assert corrections(pairs_file) == [{
        "incoming": "umefika ama bado uko job",
        "reply": "bado niko job manze",
        "source": "correction",
    }]


def test_approving_a_draft_unchanged_stores_nothing(signed_in, fresh_db, bridge, pairs_file):
    item = fresh_db.add_held(MOM, "umefika", "bado niko njiani")

    assert signed_in.post(f"/api/held/{item}/send", json={}).status_code == 200
    assert [j["text"] for j in bridge] == ["bado niko njiani"]
    assert not pairs_file.exists()


def test_dismissing_a_draft_stores_nothing(signed_in, fresh_db, pairs_file):
    """A dismissal is a signal with nowhere useful to go — see dismiss_held for
    why — so it writes nothing at all rather than a pair nothing can read."""
    item = fresh_db.add_held(MOM, "unakuja kesho", "I will confirm later.")

    assert signed_in.post(f"/api/held/{item}/dismiss").status_code == 200
    assert fresh_db.get(item)["status"] == "dismissed"
    assert not pairs_file.exists()


def test_a_failure_writing_the_pairs_file_does_not_cost_the_owner_the_reply(
        signed_in, fresh_db, bridge, pairs_file, monkeypatch):
    """The reply has already left by the time any of this runs, so a disk that
    will not take the pair must be invisible to him — and must not be able to
    leave behind a file the retrieval then cannot read."""
    write_pairs(pairs_file, [{"incoming": "umefika", "reply": "bado niko njiani"}])
    before = pairs_file.read_text(encoding="utf-8")

    def refuse(src, dst):
        raise OSError("read-only file system")

    # The rename, not the temp write: so this is also the proof that a failed
    # write leaves the file the retrieval is reading exactly as it was.
    monkeypatch.setattr(style_learning.os, "replace", refuse)

    item = fresh_db.add_held(MOM, "umefika ama bado uko job", "I am still at work.")
    r = signed_in.post(f"/api/held/{item}/send", json={"text": "bado niko job manze"})

    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert [j["text"] for j in bridge] == ["bado niko job manze"]
    assert fresh_db.get(item)["status"] == "sent"
    assert pairs_file.read_text(encoding="utf-8") == before


def test_no_message_text_reaches_a_log_line(pairs_file, monkeypatch):
    """app.log's rule: counts, flags and hashed ids only. These pairs are words
    somebody else wrote, so neither side of one may appear in a log field."""
    lines = []
    monkeypatch.setattr(style_learning, "log_event",
                        lambda event, level="info", **fields: lines.append((event, fields)))

    style_learning.learn_from_edit("umefika ama bado uko job", "I am still at work.", "bado niko job manze")

    assert lines == [("style_correction_learned", {"corrections": 1})]
    written = json.dumps(lines)
    assert "bado niko job" not in written and "umefika" not in written


# --------------------------------------------------- the file under pressure ----

def test_two_sends_landing_together_leave_valid_json_with_both_pairs(pairs_file):
    """Approvals arrive on the dashboard's threadpool, so two of them are two
    threads of one process appending to one file. Before the lock, one pair was
    lost to the other's read-modify-write and both named their temp file after
    the same process id."""
    start = threading.Barrier(2)

    def approve(thread):
        start.wait()
        for n in range(10):
            style_learning.learn_from_edit(
                f"swali la {thread} {'a' * n}", "I will look into this and revert.",
                f"wacha nicheki {thread} {'a' * n}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(approve, ("moja", "mbili")))

    kept = corrections(pairs_file)                   # json.loads: a half-written file fails here
    assert len(kept) == 20
    assert len({p["reply"] for p in kept}) == 20     # neither thread's work was overwritten
    assert list(pairs_file.parent.glob("*.tmp")) == []


def test_the_cap_holds_and_keeps_the_newest_corrections(pairs_file, monkeypatch):
    monkeypatch.setattr(style_learning, "MAX_CORRECTIONS", 3)

    for n in ("a", "b", "c", "d", "e"):
        assert style_learning.learn_from_edit(
            f"swali {n}", "I will look into this and revert.", f"wacha nicheki {n}") is True

    assert [p["reply"] for p in corrections(pairs_file)] == [
        "wacha nicheki c", "wacha nicheki d", "wacha nicheki e"]


def test_the_cap_spends_its_room_on_distinct_replies_not_repetitions(pairs_file, monkeypatch):
    """Capped, the newest wins — but saying one thing five times must not cost
    him the other things he has said. The retrieval shows the model five pairs
    at a time, and five slots holding one sentence teach one sentence."""
    monkeypatch.setattr(style_learning, "MAX_CORRECTIONS", 3)

    style_learning.learn_from_edit("swali a", "I will look into this.", "wacha nicheki kwanza")
    style_learning.learn_from_edit("swali b", "I am on my way there.", "niko njiani manze")
    for n in ("c", "d", "e"):
        # The same words again, to three different people: one correction.
        style_learning.learn_from_edit(f"swali {n}", "I will look into this.", "Wacha  nicheki kwanza")

    kept = [p["reply"] for p in corrections(pairs_file)]
    assert kept == ["niko njiani manze", "Wacha  nicheki kwanza"]


def test_pairs_learned_from_the_walk_are_left_alone_by_a_correction(pairs_file):
    walked = [{"incoming": "umefika", "reply": "bado niko njiani"}]
    write_pairs(pairs_file, walked)

    style_learning.learn_from_edit("swali a", "I will look into this.", "wacha nicheki kwanza")

    assert stored(pairs_file)[0] == walked[0]
    # And the walk's own timestamp is untouched: a correction does not rewrite
    # the style file, so it must not claim the voice was learned again.
    assert json.loads(pairs_file.read_text(encoding="utf-8"))["learned_at"] == CLOCK


def test_a_correction_survives_a_later_learn_run(pairs_file, monkeypatch, tmp_path):
    """The walk rewrites this file from scratch. His own corrections are the one
    thing in it the walk cannot rediscover, so they must outlive the rewrite."""
    import asyncio

    monkeypatch.setattr(llm, "STYLE_EXAMPLES_PATH", tmp_path / "style_examples.md")
    style_learning.learn_from_edit("swali a", "I will look into this.", "wacha nicheki kwanza")

    async def post(path, payload, timeout):
        if path == "/chats":
            body = {"chats": [{"id": MOM, "is_group": False, "left": False}]}
        else:
            body = {"messages": [{"body": "umefika", "from_me": False},
                                 {"body": "bado niko njiani", "from_me": True}]}
        return httpx.Response(200, json=body)

    monkeypatch.setattr(style_learning.bridge, "post", post)
    result = asyncio.run(style_learning.learn_style(now=CLOCK))

    assert result["pairs"] == 2      # the walked pair, and his own
    assert [p["reply"] for p in stored(pairs_file)] == ["bado niko njiani", "wacha nicheki kwanza"]
    assert len(corrections(pairs_file)) == 1


# ------------------------------------------------- the retrieval can find it ----

def test_the_retrieval_finds_a_pair_written_from_an_edit(pairs_file, monkeypatch):
    """The point of writing to this file rather than somewhere new: the next
    message benefits, with no restart. app.style_retrieval re-reads the file
    whenever its mtime or size changes, so the write has to land there."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "style_retrieval_under_test", APP_DIR / "style_retrieval.py")
    retrieval = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(retrieval)
    monkeypatch.setattr(retrieval, "PAIRS_FILE", pairs_file)
    monkeypatch.setenv("STYLE_RETRIEVAL", "true")

    write_pairs(pairs_file, [{"incoming": "habari ya kazi", "reply": "poa"}])
    assert retrieval.retrieve_examples("umefika ama bado uko job") == []     # read, and cached

    style_learning.learn_from_edit("umefika ama bado uko job", "I am still at work.", "bado niko job manze")

    assert retrieval.retrieve_examples("uko job ama umefika", k=1) == [{
        "incoming": "umefika ama bado uko job",
        "reply": "bado niko job manze",
        "source": "correction",
    }]
