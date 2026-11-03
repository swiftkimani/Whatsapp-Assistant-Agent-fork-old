"""Deciding that two ways of writing a number reach the same person.

The allowlist has always compared the last 9 digits. This is the third copy of
that question and it used to answer it differently, which is the bug below.
"""
from app import contacts


def test_a_kenyan_number_matches_its_national_form(contacts_file):
    """254712345678 and 0712345678 are one line: the trunk zero replaces the
    country code, so neither string is a suffix of the other."""
    assert contacts._same_number("254712345678", "0712345678")


def test_an_egyptian_number_still_matches_too(contacts_file):
    """Here the trunk zero is kept, so suffix comparison happened to work and
    this is the case the old rule was written for."""
    assert contacts._same_number("201012345678", "01012345678")


def test_two_different_people_do_not_match(contacts_file):
    assert not contacts._same_number("254712345678", "254799999999")


def test_a_short_number_is_never_matched_on(contacts_file):
    """Too few digits to be sure, so it is left alone rather than guessed at."""
    assert not contacts._same_number("12345", "12345")


def test_the_name_is_found_for_a_contact_saved_in_local_form(contacts_file):
    """What the bug cost: name_for() returned nothing, so the person fell back
    to the default contact mode instead of the one chosen for them, and their
    held replies were filed under a number."""
    contacts_file.write_text('{"Kawira": "0712345678"}', encoding="utf-8")
    assert contacts.name_for("254712345678@c.us") == "Kawira"
