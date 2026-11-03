"""The backend's own copy of the allowlist. The bridge gates first, but the
webhook must not depend on that being the only check."""
import pytest

from app import main


@pytest.fixture()
def allow(monkeypatch):
    def set_to(value: str):
        monkeypatch.setenv("ALLOWED_CONTACTS", value)
    return set_to


def test_an_empty_allowlist_answers_everyone_which_is_the_documented_risk(allow):
    allow("")
    assert main.is_allowed("254799999999@c.us")


def test_a_listed_number_gets_through_and_others_do_not(allow):
    allow("254712345678")
    assert main.is_allowed("254712345678@c.us")
    assert not main.is_allowed("254799999999@c.us")


def test_the_same_person_in_another_format_still_matches(allow):
    allow("0712345678")
    assert main.is_allowed("254712345678@c.us")


def test_contacts_means_everyone_in_the_address_book(allow, contacts_file):
    allow("contacts")
    # the fixture holds Mom (201000000000) and Lulu (an @lid id)
    assert main.is_allowed("201000000000@c.us")
    assert not main.is_allowed("254799999999@c.us")


def test_contacts_can_be_mixed_with_a_hand_typed_number(allow, contacts_file):
    allow("contacts,254700000001")
    assert main.is_allowed("254700000001@c.us")
    assert main.is_allowed("201000000000@c.us")


def test_an_empty_address_book_means_nobody_not_everybody(allow, contacts_file):
    contacts_file.write_text("{}", encoding="utf-8")
    allow("contacts")
    assert not main.is_allowed("254712345678@c.us")


def test_an_unreadable_address_book_means_nobody(allow, contacts_file):
    contacts_file.write_text("not json", encoding="utf-8")
    allow("contacts")
    assert not main.is_allowed("254712345678@c.us")


def test_anyone_answers_a_sender_who_is_in_no_list_at_all(allow, contacts_file):
    allow("anyone")
    # not in the address book, not hand-typed anywhere
    assert main.is_allowed("254799999999@c.us")
    assert main.is_allowed("112257593794569@lid", number="254799999999")


def test_anyone_still_refuses_groups(allow, monkeypatch):
    allow("anyone")
    monkeypatch.setattr(main, "ALLOW_GROUPS", False)
    assert not main.is_allowed("254776599915-1497087597@g.us")


def test_anyone_does_not_need_a_readable_address_book(allow, contacts_file):
    # "contacts" fails closed on a broken book; "anyone" never reads it.
    contacts_file.write_text("not json", encoding="utf-8")
    allow("anyone,contacts")
    assert main.is_allowed("254799999999@c.us")


def test_a_resolved_lid_number_is_what_gets_compared(allow):
    allow("254712345678")
    assert main.is_allowed("112257593794569@lid", number="254712345678")
    assert not main.is_allowed("112257593794569@lid", number="254799999999")


def test_a_chat_id_with_no_digits_is_refused(allow):
    allow("254712345678")
    assert not main.is_allowed("@c.us")
