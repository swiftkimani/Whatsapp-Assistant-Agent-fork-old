import pytest


def test_transition_is_atomic(fresh_db):
    item = fresh_db.add_held("1@c.us", "hi", "hello", reason="needs_owner")
    assert fresh_db.transition(item, "pending", "sending") is True
    assert fresh_db.transition(item, "pending", "sending") is False   # a double click can't send twice
    assert fresh_db.get(item)["status"] == "sending"


def test_pending_only_lists_pending_and_keeps_reason(fresh_db):
    a = fresh_db.add_held("1@c.us", "a", "r", reason="paused")
    b = fresh_db.add_held("2@c.us", "b", "r")
    fresh_db.transition(b, "pending", "dismissed")
    rows = fresh_db.pending()
    assert [r["id"] for r in rows] == [a] and rows[0]["reason"] == "paused"


def test_memory_keeps_only_the_last_messages_in_order(fresh_db):
    for i in range(10):
        fresh_db.add_message("c1", "user", f"m{i}", keep=4)
    fresh_db.add_message("c2", "user", "other", keep=4)
    assert [m["content"] for m in fresh_db.get_messages("c1")] == ["m6", "m7", "m8", "m9"]
    assert len(fresh_db.get_messages("c2")) == 1


def test_contact_modes(fresh_db):
    """Someone you have not decided about is held, so syncing an address book
    does not enrol everyone in it for automatic replies."""
    assert fresh_db.get_mode("Mom") == "ignore" and fresh_db.get_mode(None) == "ignore"
    fresh_db.set_mode("Mom", "auto")
    assert fresh_db.get_mode("mom") == "auto"          # case-insensitive
    fresh_db.set_mode("Mom", "ignore")
    assert fresh_db.all_modes() == {"mom": "ignore"}   # the choice is kept, whatever it is
    fresh_db.clear_mode("Mom")
    assert fresh_db.all_modes() == {}                  # only clearing it follows the default again


def test_changing_the_default_moves_everyone_not_decided_about(fresh_db):
    fresh_db.set_mode("Mom", "auto")
    assert fresh_db.get_mode("Dad") == "ignore" and fresh_db.get_mode("Mom") == "auto"
    fresh_db.set_default_mode("auto")
    assert fresh_db.get_mode("Dad") == "auto"          # moved
    assert fresh_db.get_mode("Mom") == "auto"


def test_a_choice_survives_the_default_moving_onto_it_and_away_again(fresh_db):
    """A row records that a choice was made, not merely that it differed from
    the default at the time. Deleting it when the two happened to agree could
    not tell "I chose this" from "I never decided", so the next change to the
    default silently moved people who had been chosen deliberately."""
    fresh_db.set_default_mode("auto")
    fresh_db.set_mode("Kawira", "auto")                 # chosen while it matches
    assert fresh_db.all_modes() == {"kawira": "auto"}
    fresh_db.set_default_mode("ignore")                 # everyone else is dropped
    assert fresh_db.get_mode("Kawira") == "auto"        # she is not
    assert fresh_db.get_mode("Someone else") == "ignore"


def test_an_unknown_default_falls_back_to_holding(fresh_db):
    fresh_db.set_setting("default_contact_mode", "nonsense")
    assert fresh_db.default_mode() == "ignore"


def test_kill_switch_defaults_to_on(fresh_db):
    assert fresh_db.auto_reply_enabled() is True
    fresh_db.set_setting("auto_reply", "0")
    assert fresh_db.auto_reply_enabled() is False


def test_old_database_is_migrated(tmp_path, monkeypatch):
    import sqlite3

    from app import db
    path = tmp_path / "old.db"
    sqlite3.connect(path).execute(
        "CREATE TABLE held (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, chat_id TEXT, incoming TEXT, reply TEXT, status TEXT DEFAULT 'pending')"
    ).connection.commit()
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init()
    row = db.get(db.add_held("1@c.us", "a", "b", reason="x", why="because", push_name="Kevo"))
    assert (row["reason"], row["why"], row["push_name"]) == ("x", "because", "Kevo")


def test_message_log_filters_and_order(fresh_db):
    fresh_db.log_message("1@c.us", "Mom", "in", "هتيجي؟", "replied")
    fresh_db.log_message("1@c.us", "Mom", "out", "ايوة", "auto")
    fresh_db.log_message("2@c.us", "Dad", "in", "hi", "held")
    assert [m["body"] for m in fresh_db.query_messages()] == ["hi", "ايوة", "هتيجي؟"]          # newest first
    assert [m["body"] for m in fresh_db.query_messages(name="mom")] == ["ايوة", "هتيجي؟"]      # case-insensitive
    assert [m["body"] for m in fresh_db.query_messages(direction="in", name="Mom")] == ["هتيجي؟"]
    assert fresh_db.query_messages(since=9_999_999_999) == []


def test_message_log_time_window_and_purge(fresh_db):
    import time
    fresh_db.log_message("1@c.us", "Mom", "in", "old", "replied")
    with fresh_db._db() as c:
        c.execute("UPDATE message_log SET ts = ?", (time.time() - 40 * 86400,))
    fresh_db.log_message("1@c.us", "Mom", "in", "new", "replied")
    assert [m["body"] for m in fresh_db.query_messages(since=time.time() - 86400)] == ["new"]
    assert fresh_db.purge_messages(days=30) == 1
    assert [m["body"] for m in fresh_db.query_messages()] == ["new"]


def test_direct_send_defaults_to_on(fresh_db):
    assert fresh_db.direct_send_enabled() is True
    fresh_db.set_setting("direct_send", "0")
    assert fresh_db.direct_send_enabled() is False


def test_a_held_row_keeps_the_name_the_sender_chose(fresh_db):
    item = fresh_db.add_held("1@c.us", "hi", "hello", push_name="Kevo Mech")
    assert fresh_db.get(item)["push_name"] == "Kevo Mech"
    # Nothing supplied is an empty string, not a null the card has to defend against.
    assert fresh_db.get(fresh_db.add_held("1@c.us", "hi", "hello"))["push_name"] == ""


def test_pending_in_chat_sees_one_conversation_and_only_what_is_still_waiting(fresh_db):
    older = fresh_db.add_held("1@c.us", "a", "r")
    newer = fresh_db.add_held("1@c.us", "b", "r")
    theirs = fresh_db.add_held("2@c.us", "c", "r")
    handled = fresh_db.add_held("1@c.us", "d", "r")
    fresh_db.transition(handled, "pending", "dismissed")

    assert [r["id"] for r in fresh_db.pending_in_chat("1@c.us")] == [newer, older]
    assert [r["id"] for r in fresh_db.pending_in_chat("2@c.us")] == [theirs]
    # A draft with no conversation has no neighbours to be confused with.
    assert fresh_db.pending_in_chat("") == []


def test_set_modes_writes_a_whole_selection_at_once(fresh_db):
    assert fresh_db.set_modes(["Mom", "Dad", "Lulu"], "hold") == 3
    assert fresh_db.all_modes() == {"mom": "hold", "dad": "hold", "lulu": "hold"}


def test_set_modes_normalises_and_de_duplicates_the_names(fresh_db):
    """Stored the way set_mode stores them, so the two cannot disagree about
    whose row a choice belongs to."""
    assert fresh_db.set_modes([" Mom ", "mom", "MOM", "", "   "], "auto") == 1
    assert fresh_db.all_modes() == {"mom": "auto"} and fresh_db.get_mode("Mom") == "auto"


def test_set_modes_rejects_a_mode_it_does_not_know_and_writes_nothing(fresh_db):
    with pytest.raises(ValueError):
        fresh_db.set_modes(["Mom", "Dad"], "bogus")
    assert fresh_db.all_modes() == {}


def test_set_modes_replaces_an_earlier_choice(fresh_db):
    fresh_db.set_mode("Mom", "auto")
    assert fresh_db.set_modes(["Mom", "Dad"], "ignore") == 2
    assert fresh_db.get_mode("Mom") == "ignore" and fresh_db.get_mode("Dad") == "ignore"


def test_set_modes_with_nothing_to_write_writes_nothing(fresh_db):
    assert fresh_db.set_modes([], "hold") == 0
    assert fresh_db.set_modes(["", "  "], "hold") == 0
    assert fresh_db.all_modes() == {}


def test_an_existing_database_of_messages_is_indexed_for_search_exactly_once(tmp_path, monkeypatch):
    """A database written before the search index existed comes out of init()
    searchable, with the month of messages it already holds indexed rather
    than only what arrives afterwards — and the next boot indexes nothing,
    which on a full log would otherwise mean re-reading every message on every
    restart."""
    import sqlite3
    import time

    from app import db
    path = tmp_path / "before-search.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE message_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, "
                "chat_id TEXT, name TEXT, direction TEXT, body TEXT, outcome TEXT)")
    old.executemany(
        "INSERT INTO message_log (ts, chat_id, name, direction, body, outcome) VALUES (?, ?, ?, ?, ?, ?)",
        [(time.time(), "1@c.us", "Landlord", "in", "the water bill is 2,400", "replied"),
         (time.time(), "2@c.us", "Kevo", "in", "niaje bro", "replied")])
    old.commit()
    old.close()
    monkeypatch.setattr(db, "DB_PATH", path)

    db.init()
    assert [r["name"] for r in db.search_messages("water bill")] == ["Landlord"]
    with db._db() as c:
        assert db._add_message_search(c) == 0      # a second boot has nothing to do

    # And the triggers carry on from there, so new messages need no backfill.
    db.log_message("3@c.us", "Dee", "in", "another water bill came", "held")
    assert [r["name"] for r in db.search_messages("water bill")] == ["Dee", "Landlord"]
