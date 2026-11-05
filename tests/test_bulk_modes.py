"""Choosing who the assistant answers, for many people in one call.

One person per request meant an address book of hundreds took hundreds of
requests. These cover the plural route: what it writes, what it refuses, and
what it admits it could not do.
"""
import json

import pytest

from app import controls

BOOK = ["Mum", "Kawira", "Otieno", "Wanjiku", "Baba", "Njeri", "Kip", "Zawadi"]


@pytest.fixture()
def book(contacts_file):
    """A fuller address book than the two-name default, since the point of this
    route is many people at once."""
    contacts_file.write_text(
        json.dumps({name: f"2547{i:08d}" for i, name in enumerate(BOOK, start=1)}), encoding="utf-8")
    return BOOK


@pytest.fixture()
def bulk_writer(fresh_db, monkeypatch):
    """db.set_modes is arriving in app/db.py in a parallel change.

    Until it is there, stand in for it with exactly what it promises — validate
    the mode, normalise and de-duplicate the names, return how many rows were
    written — built on the single-row writer that already exists, so these
    tests exercise the route and the real database rather than waiting on
    another file.
    """
    if hasattr(fresh_db, "set_modes"):
        return fresh_db

    def set_modes(names: list[str], mode: str) -> int:
        if mode not in fresh_db.MODES:
            raise ValueError(f"bad mode: {mode}")
        written: list[str] = []
        for name in names:
            key = (name or "").strip().lower()
            if key and key not in written:
                written.append(key)
                fresh_db.set_mode(key, mode)
        return len(written)

    monkeypatch.setattr(fresh_db, "set_modes", set_modes, raising=False)
    return fresh_db


def set_modes(client, names, mode="auto"):
    return client.post("/api/contacts/modes", json={"names": names, "mode": mode})


def test_many_people_are_chosen_in_one_call(signed_in, book, bulk_writer):
    chosen = ["Mum", "Kawira", "Otieno", "Njeri", "Kip"]
    r = set_modes(signed_in, chosen, "auto")
    assert r.status_code == 200
    assert r.json() == {"changed": 5, "mode": "auto", "unknown": []}

    people = signed_in.get("/api/contact-modes").json()
    assert {c["name"] for c in people["contacts"] if c["mode"] == "auto"} == set(chosen)
    assert people["auto_count"] == 5          # and nobody else moved with them


def test_a_name_typed_in_any_case_reaches_the_person_stored(signed_in, book, bulk_writer):
    assert set_modes(signed_in, ["mum", " KAWIRA "], "hold").json()["changed"] == 2
    modes = {c["name"]: c["mode"] for c in signed_in.get("/api/contact-modes").json()["contacts"]}
    assert modes["Mum"] == "hold" and modes["Kawira"] == "hold"


def test_a_name_nobody_matches_is_reported_rather_than_stored(signed_in, book, bulk_writer):
    """A mode row is keyed on the name, so a typo would write a row that
    matches no chat and quietly do nothing. The owner is told instead."""
    r = set_modes(signed_in, ["Mum", "Kawria", "Kawira"], "hold")
    assert r.status_code == 200
    assert r.json() == {"changed": 2, "mode": "hold", "unknown": ["Kawria"]}
    assert set(bulk_writer.all_modes()) == {"mum", "kawira"}


def test_nothing_is_written_when_no_name_matches(signed_in, book, bulk_writer):
    r = set_modes(signed_in, ["Nobody", "Someone Else", "Nobody"], "auto")
    assert r.json() == {"changed": 0, "mode": "auto", "unknown": ["Nobody", "Someone Else"]}
    assert bulk_writer.all_modes() == {}
    kinds = [i["kind"] for i in signed_in.get("/api/audit").json()["items"]]
    assert "mode_changed" not in kinds       # no "0 people" line in the feed


def test_the_same_person_listed_twice_is_counted_once(signed_in, book, bulk_writer):
    assert set_modes(signed_in, ["Mum", "mum", " Mum "], "ignore").json()["changed"] == 1


def test_a_nonsense_mode_is_refused(signed_in, book, bulk_writer):
    assert set_modes(signed_in, ["Mum"], "bogus").status_code == 422
    assert bulk_writer.all_modes() == {}


def test_an_empty_list_is_refused(signed_in, book, bulk_writer):
    assert set_modes(signed_in, [], "auto").status_code == 422


def test_a_blank_or_overlong_name_is_refused(signed_in, book, bulk_writer):
    assert set_modes(signed_in, ["Mum", ""], "auto").status_code == 422
    assert set_modes(signed_in, ["x" * 61], "auto").status_code == 422
    assert bulk_writer.all_modes() == {}


def test_more_names_than_the_ceiling_are_refused(signed_in, book, bulk_writer):
    assert set_modes(signed_in, ["Mum"] * (controls.MAX_BULK_NAMES + 1), "auto").status_code == 422
    # The ceiling itself is allowed: a whole address book has to fit.
    assert set_modes(signed_in, ["Mum"] * controls.MAX_BULK_NAMES, "auto").status_code == 200


def test_setting_modes_in_bulk_needs_the_password(client, book):
    """Every other route here is behind the password; so is this one, which is
    the one that can switch the assistant loose on a whole list."""
    r = client.post("/api/contacts/modes", json={"names": ["Mum"], "mode": "auto"})
    assert r.status_code == 401


def test_a_bulk_change_to_auto_is_written_to_the_activity_feed(signed_in, book, bulk_writer):
    """"auto" answers people with no approval, so the owner must be able to
    see afterwards how many people it was done to, and which mode."""
    set_modes(signed_in, ["Mum", "Kawira", "Otieno"], "auto")
    items = signed_in.get("/api/audit").json()["items"]
    assert items[0]["kind"] == "mode_changed"
    assert items[0]["target"] == "3 people: auto"
    # One summary line, not one per person: a few hundred would push
    # everything else out of the feed.
    assert [i["kind"] for i in items].count("mode_changed") == 1


def test_one_person_is_counted_in_words_the_feed_can_read(signed_in, book, bulk_writer):
    set_modes(signed_in, ["Mum"], "hold")
    assert signed_in.get("/api/audit").json()["items"][0]["target"] == "1 person: hold"
