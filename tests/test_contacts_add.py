"""Adding one person by hand, which is what makes a contact settable without
a full sync of the phone's address book."""
import json

import pytest

from app import contacts


def test_adding_someone_puts_them_on_the_list(contacts_file):
    assert contacts.add("Kawira", "+254 712 345 678") == "Kawira"
    assert json.loads(contacts_file.read_text())["Kawira"] == "254712345678"


def test_the_name_is_tidied_but_kept_as_written(contacts_file):
    assert contacts.add("  Kawira   Mwende ", "254712345678") == "Kawira Mwende"


def test_an_lid_privacy_id_is_a_valid_recipient(contacts_file):
    contacts.add("Quiet one", "112257593794560@lid")
    assert json.loads(contacts_file.read_text())["Quiet one"] == "112257593794560@lid"


def test_adding_never_overwrites_someone_already_there(contacts_file):
    """contacts.json is the list the dashboard may message, so a careless
    write could send a message meant for one person to another."""
    with pytest.raises(ValueError, match="already on the list"):
        contacts.add("mom", "254700000000")
    assert json.loads(contacts_file.read_text())["Mom"] == "201000000000"


def test_the_same_line_in_another_format_is_not_added_twice(contacts_file):
    contacts.add("Kawira", "254712345678")
    with pytest.raises(ValueError, match="already saved as Kawira"):
        contacts.add("Kawira at work", "+254 712 345 678")


def test_a_national_number_is_refused_rather_than_stored_unreachable(contacts_file):
    """The bridge resolves a stored number with getNumberId(), which wants the
    international form. "0712…" resolves to nobody, so the entry would sit on
    the list looking fine and fail only when a message was actually sent."""
    with pytest.raises(ValueError, match="country code"):
        contacts.add("Kawira", "0712345678")


@pytest.mark.parametrize("name, number", [("", "254712345678"), ("Kawira", "12"), ("Kawira", "")])
def test_nonsense_is_refused_with_something_readable(contacts_file, name, number):
    with pytest.raises(ValueError):
        contacts.add(name, number)


def test_a_contact_saved_in_local_format_still_resolves_to_their_name(contacts_file):
    """The bug this found: a Kenyan number keeps its last 9 digits but swaps
    254 for a leading 0, so comparing by suffix never matched and the person
    silently got the default mode instead of the one set for them."""
    contacts_file.write_text('{"Kawira": "0712345678"}', encoding="utf-8")
    assert contacts.name_for("254712345678@c.us") == "Kawira"
    assert contacts.name_for("254799999999@c.us") is None


def test_the_endpoint_adds_and_returns_the_new_list(signed_in):
    r = signed_in.post("/api/contacts", json={"name": "Kawira", "number": "254712345678"})
    assert r.status_code == 200
    assert r.json()["name"] == "Kawira"
    assert "Kawira" in r.json()["contacts"]


def test_the_endpoint_explains_a_refusal_in_a_sentence(signed_in):
    r = signed_in.post("/api/contacts", json={"name": "Mom", "number": "254712345678"})
    assert r.status_code == 400 and "already on the list" in r.json()["detail"]


def test_adding_needs_a_session(client):
    assert client.post("/api/contacts", json={"name": "X", "number": "254712345678"}).status_code == 401


def test_someone_added_by_hand_can_then_be_set_to_reply(signed_in):
    """The whole point: added, then given a setting of their own, with no
    sync and no code change in between."""
    signed_in.post("/api/contacts", json={"name": "Kawira", "number": "254712345678"})
    r = signed_in.post("/api/contact-modes", json={"name": "kawira", "mode": "auto"})
    assert r.status_code == 200 and r.json() == {"name": "Kawira", "mode": "auto"}
