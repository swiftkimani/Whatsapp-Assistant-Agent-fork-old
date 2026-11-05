"""Searching the message log by what was said.

The owner has around 310 conversations in here and, until this existed, no way
to ask what any of them said. What the index really matches is measured in
these tests rather than assumed — including the places it does not match,
which are the ones worth knowing about before trusting an empty result.
"""
import os
import time

import pytest

TECHNOLOGIST = "\U0001F469\U0001F3FD\u200D\U0001F4BB"   # woman technologist, medium skin tone
LAPTOP = "\U0001F4BB"
SKIN_TONE = "\U0001F3FD"
ZWJ = "\u200D"
CRYING_LAUGHING = "\U0001F602"

CONVERSATION = [
    # (chat_id, name, direction, body, outcome)
    ("254711000001@c.us", "Landlord", "in", "Habari, the water bill for October is 2,400", "replied"),
    ("254711000001@c.us", "Landlord", "out", "Sawa, nitatuma pesa ya maji kesho asubuhi", "auto"),
    ("254711000002@c.us", "Kevo", "in", "Niaje bro, uko aje? Tuonane kesho", "replied"),
    ("254711000003@c.us", "Mom", "in", "Utakuja harusi jumamosi? Lazima nijue leo", "held"),
    ("254711000004@c.us", "Dee", "in", "deposit is 100% refundable, see water_bill.pdf", "replied"),
    ("254711000005@c.us", "Zu", "in", 'she said "pay it today" and left', "replied"),
    ("254711000006@c.us", "Ciru", "in", "the café on Ngong Road", "replied"),
]


@pytest.fixture()
def logged(fresh_db):
    """The message log with a handful of real-looking conversations in it."""
    for row in CONVERSATION:
        fresh_db.log_message(*row)
    return fresh_db


def found(db, term, **filters) -> list[str]:
    """Whose messages came back, in order."""
    return [r["name"] for r in db.search_messages(term, **filters)]


# ---------------------------------------------------- what matches ----

def test_a_word_in_the_middle_of_a_message_is_found(logged):
    assert found(logged, "bill") == ["Dee", "Landlord"]


def test_matching_ignores_case_for_ascii(logged):
    assert found(logged, "HABARI") == found(logged, "habari") == ["Landlord"]


def test_matching_ignores_case_and_accents_beyond_ascii(logged):
    """More than LIKE would give: SQLite's LIKE folds ASCII only, so none of
    these three spellings would find the other two."""
    assert found(logged, "cafe") == found(logged, "café") == found(logged, "CAFÉ") == ["Ciru"]


def test_a_kiswahili_word_is_found(logged):
    assert found(logged, "maji") == ["Landlord"]
    assert found(logged, "nitatuma") == ["Landlord"]


def test_a_sheng_word_is_found(logged):
    assert found(logged, "niaje") == ["Kevo"]
    assert found(logged, "uko aje") == ["Kevo"]


def test_the_last_word_typed_matches_as_a_prefix(logged):
    """So the results are useful while he is still typing."""
    assert found(logged, "nitat") == ["Landlord"]
    # Dee's "water_bill.pdf" comes too: an underscore is a separator, so that
    # is the two words "water" and "bill" as far as the index is concerned.
    assert found(logged, "water bi") == ["Dee", "Landlord"]


def test_part_of_a_word_that_is_not_its_start_is_not_found(logged):
    """This is a word index, not a substring scan. Pinned because an empty
    result here is the behaviour, not a bug to go looking for."""
    assert found(logged, "ater") == []
    assert found(logged, "abari") == []


def test_a_word_that_differs_at_the_front_is_a_different_word(logged):
    """Kiswahili marks number on the prefix, so a word index cuts across it:
    "mtoto" and "watoto" never find each other, and the stem they share finds
    neither. Typing a prefix is the way in, and it has to be the word's own
    prefix. Measured, and the limitation most likely to bite this corpus."""
    logged.log_message("254711000012@c.us", "Shiko", "in", "watoto wamerudi shule", "replied")
    assert found(logged, "watoto") == ["Shiko"]
    assert found(logged, "mtoto") == []
    assert found(logged, "toto") == []
    assert found(logged, "wato") == ["Shiko"]


def test_the_apostrophe_in_a_kiswahili_word_splits_it_in_two(logged):
    """unicode61 counts an apostrophe as one of the spaces between words, so
    "ng'ombe" is indexed as "ng" and "ombe" and the spelling without it is a
    different word. Pinned because an empty result for "ngombe" is the
    behaviour and not a bug to go looking for."""
    logged.log_message("254711000013@c.us", "Shiko", "in", "ng'ombe wa shamba amepotea", "replied")
    assert found(logged, "ng'ombe") == ["Shiko"]
    assert found(logged, "ngombe") == []
    assert found(logged, "ombe") == ["Shiko"]   # the tail is a word of its own here


def test_several_words_are_searched_as_a_phrase(logged):
    """Side by side, in that order — not merely both present somewhere."""
    assert found(logged, "water bill") == ["Dee", "Landlord"]
    assert found(logged, "bill water") == []


# -------------------------------------------- emoji and multi-byte ----

def test_an_emoji_on_its_own_cannot_be_searched_for(logged):
    """Most emoji are the spaces between words as far as the index is
    concerned, so this finds nothing rather than everything."""
    logged.log_message("254711000008@c.us", "Em", "in", f"haha {CRYING_LAUGHING}{CRYING_LAUGHING} sawa", "replied")
    assert found(logged, CRYING_LAUGHING * 2) == []
    assert found(logged, "haha") == ["Em"]


def test_a_skin_tone_is_the_one_emoji_piece_the_index_does_hold(logged):
    """The five skin-tone modifiers are token characters to unicode61 while
    the pictures around them are not, so searching for a toned emoji matches
    on the tone alone — it finds any message carrying that tone, whatever
    emoji wore it. Pinned because it is surprising, not because it is wanted."""
    logged.log_message("254711000009@c.us", "Em", "in", f"on it {TECHNOLOGIST}", "replied")
    logged.log_message("254711000010@c.us", "Jo", "in", f"\U0001F44D{SKIN_TONE} sawa", "replied")
    assert sorted(found(logged, TECHNOLOGIST)) == ["Em", "Jo"]


# ------------------------------------- characters that could misbehave ----

def test_a_percent_sign_is_a_separator_and_never_a_wildcard(logged):
    """In a LIKE pattern "%" would match the whole table. Here it is one of the
    spaces between words: a term made of them matches nothing, and "100%"
    searches for the number."""
    assert found(logged, "%%") == []
    assert found(logged, "%deposit%") == ["Dee"]
    assert found(logged, "100%") == ["Dee"]


def test_an_underscore_is_a_separator_and_never_a_wildcard(logged):
    assert found(logged, "__") == []
    assert found(logged, "water_bill") == ["Dee", "Landlord"]


def test_a_term_with_a_quote_in_it_finds_the_message_and_breaks_nothing(logged):
    assert found(logged, 'said "pay') == ["Zu"]
    assert found(logged, '"pay it today"') == ["Zu"]
    assert found(logged, '""') == []


def test_the_index_query_language_is_searched_as_words(logged):
    """AND, OR, NOT, NEAR and a bare "*" are FTS5's own vocabulary. Typed into
    a search box they are text, and none of them may reach it as syntax."""
    logged.log_message("254711000011@c.us", "Ann", "in", "bring NEAR AND OR * the thing", "replied")
    assert found(logged, "NEAR AND OR") == ["Ann"]
    assert found(logged, "water OR niaje") == []
    assert found(logged, "* *") == []


# ------------------------------------------------------- the filters ----

def test_the_person_filter_still_applies(logged):
    assert found(logged, "kesho", name="Landlord") == ["Landlord"]
    assert found(logged, "kesho", name="landlord") == ["Landlord"]   # case-insensitive
    assert found(logged, "kesho", name="Kevo") == ["Kevo"]
    assert found(logged, "kesho", name="Mom") == []


def test_the_period_filter_still_applies(logged):
    now = time.time()
    assert found(logged, "bill", since=now - 60) == ["Dee", "Landlord"]
    assert found(logged, "bill", since=now + 60) == []
    assert found(logged, "bill", until=now - 60) == []


def test_the_direction_filter_still_applies(logged):
    assert found(logged, "kesho", direction="out") == ["Landlord"]
    assert found(logged, "kesho", direction="in") == ["Kevo"]
    assert found(logged, "kesho", direction="both") == ["Kevo", "Landlord"]   # unknown: no filter


def test_newest_first(logged):
    assert found(logged, "kesho") == ["Kevo", "Landlord"]


# --------------------------------------------- bounds and refusals ----

def test_the_limit_is_bounded_by_the_same_ceiling_the_rest_of_the_log_uses(fresh_db):
    with fresh_db._db() as c:
        c.executemany(
            "INSERT INTO message_log (ts, chat_id, name, direction, body, outcome) VALUES (?, ?, ?, ?, ?, ?)",
            [(time.time(), "1@c.us", "Mom", "in", f"deposit note {i}", "") for i in range(600)])
    assert len(fresh_db.search_messages("deposit", limit=10_000)) == 500
    assert len(fresh_db.search_messages("deposit", limit=0)) == 1


def test_an_empty_or_one_character_term_is_refused(logged):
    for term in ("", " ", "a", " a ", "\n"):
        with pytest.raises(ValueError):
            logged.search_messages(term)


def test_a_term_that_is_only_spaces_around_one_letter_cannot_sneak_past(logged):
    """"a " is two characters and a prefix match, which would be every message
    in the log. The term is collapsed before it is measured."""
    with pytest.raises(ValueError):
        logged.search_messages("a ")


# ------------------------------------------------- what comes back ----

def test_a_result_carries_the_chat_id_the_outcome_and_a_snippet(logged):
    row = logged.search_messages("water bill", name="Landlord")[0]
    assert row["chat_id"] == "254711000001@c.us"
    assert row["direction"] == "in" and row["outcome"] == "replied"
    assert row["name"] == "Landlord" and row["ts"] > 0 and row["id"] > 0
    assert "water bill" in row["snippet"]
    assert row["body"] == "Habari, the water bill for October is 2,400"


def test_a_snippet_is_shortened_around_the_match(fresh_db):
    body = " ".join(f"word{i:02d}" for i in range(40)) + " needle"
    fresh_db.log_message("1@c.us", "Mom", "in", body, "")
    snippet = fresh_db.search_messages("needle")[0]["snippet"]
    assert snippet.startswith(fresh_db.SNIPPET_ELLIPSIS) and snippet.endswith("needle")
    assert len(snippet) < len(body)


def test_a_snippet_never_starts_half_way_through_an_emoji(fresh_db):
    """snippet() cuts on token boundaries and the skin-tone modifier inside
    this emoji is a token, so the raw window really does begin on a skin tone
    and a joiner with no person in front of them. Both ends are repaired to
    whole characters before anyone sees them."""
    body = "w01 " + TECHNOLOGIST + " " + " ".join(f"w{i:02d}" for i in range(2, 12)) + " needle"
    fresh_db.log_message("1@c.us", "Mom", "in", body, "")
    with fresh_db._db() as c:
        raw = c.execute(
            "SELECT snippet(message_log_fts, 0, '', '', ?, ?) FROM message_log_fts "
            "WHERE message_log_fts MATCH ?",
            (fresh_db.SNIPPET_ELLIPSIS, fresh_db.SNIPPET_TOKENS, '"needle" *')).fetchone()[0]
    assert raw.lstrip(fresh_db.SNIPPET_ELLIPSIS).startswith(SKIN_TONE)   # the bug, still there in SQLite

    snippet = fresh_db.search_messages("needle")[0]["snippet"]
    assert snippet == f"{fresh_db.SNIPPET_ELLIPSIS}{LAPTOP} " + " ".join(f"w{i:02d}" for i in range(2, 12)) + " needle"
    assert SKIN_TONE not in snippet and ZWJ not in snippet


def test_pages_carry_on_from_the_cursor_without_repeating_a_row(logged):
    first = logged.search_messages("kesho", limit=1)
    second = logged.search_messages("kesho", limit=1, before_id=first[0]["id"])
    third = logged.search_messages("kesho", limit=1, before_id=second[0]["id"])
    assert [r["name"] for r in first] == ["Kevo"]
    assert [r["name"] for r in second] == ["Landlord"]
    assert third == []


# ------------------------------------------- the index stays in step ----

def test_a_message_older_than_retention_is_not_found_because_it_was_purged(fresh_db):
    fresh_db.log_message("1@c.us", "Landlord", "in", "the water bill from August", "replied")
    with fresh_db._db() as c:
        c.execute("UPDATE message_log SET ts = ?", (time.time() - 40 * 86400,))
    assert found(fresh_db, "water bill") == ["Landlord"]   # backdating does not lose it

    assert fresh_db.purge_messages(days=30) == 1
    assert found(fresh_db, "water bill") == []


def test_an_edited_message_is_searched_by_its_new_text(fresh_db):
    fresh_db.log_message("1@c.us", "Mom", "in", "the first wording", "")
    with fresh_db._db() as c:
        c.execute("UPDATE message_log SET body = ?", ("the second wording",))
    assert found(fresh_db, "second wording") == ["Mom"]
    assert found(fresh_db, "first wording") == []


# -------------------------------------------------------- the route ----

def test_the_route_requires_authentication(client):
    assert client.post("/api/messages/search", json={"term": "water"}).status_code == 401


def test_the_route_returns_items_a_cursor_and_who_each_message_was_with(signed_in):
    from app import db
    db.log_message("201000000000@c.us", "", "in", "the water bill is due", "replied")
    db.log_message("254700000000@c.us", "Landlord", "in", "water bill paid", "replied")

    body = signed_in.post("/api/messages/search", json={"term": "water bill"}).json()
    assert body["next_cursor"] is None
    assert [i["name"] for i in body["items"]] == ["Landlord", "Mom"]   # the number resolved to a contact
    first = body["items"][0]
    assert first["chat_id"] == "254700000000@c.us"
    assert "water bill" in first["snippet"] and first["direction"] == "in"
    assert first["outcome"] == "replied" and first["id"] > 0


def test_the_route_hands_back_a_cursor_when_the_page_is_full(signed_in):
    from app import db
    for i in range(3):
        db.log_message(f"25470000000{i}@c.us", "Mom", "in", f"water bill {i}", "")

    page = signed_in.post("/api/messages/search", json={"term": "water", "limit": 2}).json()
    assert len(page["items"]) == 2 and page["next_cursor"] == page["items"][-1]["id"]

    rest = signed_in.post("/api/messages/search",
                          json={"term": "water", "limit": 2, "before_id": page["next_cursor"]}).json()
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None
    assert not {i["id"] for i in page["items"]} & {i["id"] for i in rest["items"]}


def test_the_route_refuses_a_term_too_short_to_mean_anything(signed_in):
    assert signed_in.post("/api/messages/search", json={"term": "a"}).status_code == 400
    assert signed_in.post("/api/messages/search", json={"term": ""}).status_code == 422
    too_long = signed_in.post("/api/messages/search", json={"term": "x" * 500})
    assert too_long.status_code == 422


def test_the_route_filters_by_contact_period_and_direction(signed_in):
    from app import db
    db.log_message("201000000000@c.us", "Mom", "in", "pesa ya maji", "replied")
    db.log_message("201000000000@c.us", "Mom", "out", "sawa, pesa itakuja", "auto")
    db.log_message("254700000000@c.us", "Landlord", "in", "pesa ya deposit", "held")

    def items(**extra):
        return signed_in.post("/api/messages/search", json={"term": "pesa", **extra}).json()["items"]

    assert len(items()) == 3
    assert {i["name"] for i in items(contact="mom")} == {"Mom"}
    assert [i["direction"] for i in items(direction="sent")] == ["out"]
    assert [i["direction"] for i in items(direction="received")] == ["in", "in"]
    assert len(items(range="today")) == 3
    assert items(range="yesterday") == []


def test_the_route_rejects_a_contact_nobody_knows(signed_in):
    response = signed_in.post("/api/messages/search", json={"term": "water", "contact": "Nobody"})
    assert response.status_code == 400 and "Unknown contact" in response.json()["detail"]


def test_the_route_rejects_a_period_or_direction_it_does_not_know(signed_in):
    assert signed_in.post("/api/messages/search",
                          json={"term": "water", "range": "all_time"}).status_code == 422
    assert signed_in.post("/api/messages/search",
                          json={"term": "water", "direction": "inbound"}).status_code == 422
    assert signed_in.post("/api/messages/search",
                          json={"term": "water", "limit": 5000}).status_code == 422


def test_the_search_term_cannot_reach_a_request_log(signed_in):
    """It goes in a POST body for one reason: uvicorn files the query string of
    every request it serves, and on the host that log is the container's. A
    search for "landlord deposit" must not be readable there, or in app.log,
    which only ever gets the length and the count."""
    assert signed_in.get("/api/messages/search?term=landlord").status_code == 405

    secret = "pregnancy"
    from app import db
    db.log_message("201000000000@c.us", "Mom", "in", f"the {secret} test", "held")
    assert signed_in.post("/api/messages/search", json={"term": secret}).status_code == 200

    log = open(os.environ["APP_LOG_FILE"], encoding="utf-8").read()
    assert "message_search" in log and secret not in log
