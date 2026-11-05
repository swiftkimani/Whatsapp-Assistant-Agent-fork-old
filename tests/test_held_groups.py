"""The held queue grouped by the person it is waiting on.

Fifty drafts in a flat list is not a backlog anyone works through, so these
cover the grouped view and the two capped counts that made a real backlog
invisible: a Held badge that stopped at fifty and a query that could not see
past the fiftieth row.

They also cover what the owner saw on the Held tab once it was full: the same
answer repeated down the page, a conversation headed "Unknown contact" whose
number was known all along, and a card made unreadable by a whole long message.
"""
import httpx
import pytest

from app import approvals

MOM = "201000000000@c.us"            # in the test contacts.json
LULU = "112257593794569@lid"         # also in it, as a privacy id
LANDLORD = "254712345678@c.us"       # in nobody's contacts
STRANGER = "999888777666555@lid"     # a privacy id in nobody's contacts either


def held(store, chat_id, incoming, reply, ts, **row):
    """One waiting draft whose arrival time is set, so the ordering these tests
    assert is the stored time and not a guess about how fast the inserts ran."""
    item = store.add_held(chat_id, incoming, reply, **row)
    with store._db() as c:
        c.execute("UPDATE held SET ts = ? WHERE id = ?", (ts, item))
    return item


@pytest.fixture()
def bridge(monkeypatch):
    """Stands in for the whatsapp-web.js bridge and keeps what it was asked to
    send, so a test can say how many messages really went out."""
    sent = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return httpx.Response(200, json={"status": "sent"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(approvals.httpx, "post", post)
    return sent


def cards(signed_in, index=0):
    return signed_in.get("/api/held/by-contact").json()["groups"][index]["items"]


def test_three_conversations_become_three_groups_with_their_counts(signed_in, fresh_db):
    held(fresh_db, MOM, "are you coming?", "on my way", 100)
    held(fresh_db, MOM, "and the keys?", "in my bag", 200)
    held(fresh_db, LANDLORD, "rent is due", "paying friday", 150)
    held(fresh_db, LULU, "free later?", "after six", 120)

    r = signed_in.get("/api/held/by-contact").json()
    assert r["contacts"] == 3 and r["total"] == 4 and r["shown"] == 4
    assert {g["chat_id"]: g["count"] for g in r["groups"]} == {MOM: 2, LANDLORD: 1, LULU: 1}

    by_chat = {g["chat_id"]: g for g in r["groups"]}
    assert by_chat[MOM]["name"] == "Mom" and by_chat[LULU]["name"] == "Lulu"
    # Nobody saved under that number, so the heading is a number the owner could
    # dial rather than a chat id, and every draft in the group carries it too.
    assert by_chat[LANDLORD]["name"] == "" and by_chat[LANDLORD]["from"] == "+254712345678"
    assert by_chat[MOM]["items"][0]["name"] == "Mom"


def test_groups_lead_with_the_most_recently_held_draft(signed_in, fresh_db):
    """Not by name, not by chat id, and not by the order they were inserted."""
    held(fresh_db, LULU, "free later?", "after six", 300)
    held(fresh_db, MOM, "are you coming?", "on my way", 100)
    held(fresh_db, LANDLORD, "rent is due", "paying friday", 200)

    groups = signed_in.get("/api/held/by-contact").json()["groups"]
    assert [g["chat_id"] for g in groups] == [LULU, LANDLORD, MOM]
    assert [g["latest_ts"] for g in groups] == [300, 200, 100]


def test_drafts_inside_a_group_are_newest_first(signed_in, fresh_db):
    first = held(fresh_db, MOM, "are you coming?", "on my way", 100)
    second = held(fresh_db, MOM, "and the keys?", "in my bag", 200)
    third = held(fresh_db, MOM, "which bag?", "the blue one", 300)

    group = signed_in.get("/api/held/by-contact").json()["groups"][0]
    assert [i["id"] for i in group["items"]] == [third, second, first]
    # Each draft still shows the message it is answering, which is the point of
    # opening a person rather than reading a wall of replies.
    assert group["items"][0]["incoming"] == "which bag?"


def test_total_counts_the_backlog_while_shown_counts_what_came_back(signed_in, fresh_db):
    for i in range(7):
        held(fresh_db, f"2547000000{i:02d}@c.us", "hi", "hello", 100 + i)

    r = signed_in.get("/api/held/by-contact?limit=3").json()
    assert r["total"] == 7 and r["shown"] == 3 and r["contacts"] == 3


def test_the_held_badge_counts_past_the_old_fifty_row_cap(signed_in, fresh_db):
    """What the cap cost: the badge counted the rows the query returned, so the
    owner's one signal that it was getting worse read "50" either way."""
    for i in range(53):
        held(fresh_db, MOM, "are you coming?", "on my way", 100 + i)

    assert signed_in.get("/api/status").json()["held"] == 53
    assert len(signed_in.get("/api/held").json()["items"]) == 50   # the flat page is unchanged
    r = signed_in.get("/api/held/by-contact").json()
    assert r["total"] == 53 and r["shown"] == 53
    assert r["groups"][0]["count"] == 53


def test_both_views_say_when_each_draft_arrived(signed_in, fresh_db):
    held(fresh_db, MOM, "are you coming?", "on my way", 1762000000.0)

    assert signed_in.get("/api/held").json()["items"][0]["ts"] == 1762000000.0
    group = signed_in.get("/api/held/by-contact").json()["groups"][0]
    assert group["items"][0]["ts"] == 1762000000.0 and group["latest_ts"] == 1762000000.0


def test_a_draft_with_no_conversation_stands_on_its_own(signed_in, fresh_db):
    held(fresh_db, MOM, "are you coming?", "on my way", 100)
    first_orphan = held(fresh_db, "", "", "something to send", 200)
    second_orphan = held(fresh_db, "", "", "and another", 300)

    r = signed_in.get("/api/held/by-contact").json()
    # Two drafts with no chat id are not evidence of one person, so they are two
    # groups rather than one bucket keyed on the empty string.
    assert r["contacts"] == 3 and r["shown"] == 3
    lone = r["groups"][0]
    assert lone["chat_id"] == "" and lone["count"] == 1
    assert [i["id"] for i in lone["items"]] == [second_orphan]
    assert [i["id"] for i in r["groups"][1]["items"]] == [first_orphan]
    assert lone["from"]          # a heading that says something, not a blank


def test_the_grouped_view_needs_a_login(client):
    assert client.get("/api/held/by-contact").status_code == 401


# ---- one answer, one card ----

def test_a_burst_of_identical_drafts_becomes_one_card_that_counts_its_repeats(signed_in, fresh_db):
    """Three messages in a row about one thing drew three drafts saying the
    same words. The owner's words for it: "repetive answers on the held"."""
    first = held(fresh_db, MOM, "are you there?", "On my way.", 100)
    second = held(fresh_db, MOM, "hello?", "on my  way.", 200)      # same words, spaced differently
    third = held(fresh_db, MOM, "mum?", "ON MY WAY.", 300)          # and capitalised differently

    group = signed_in.get("/api/held/by-contact").json()["groups"][0]
    assert [i["id"] for i in group["items"]] == [third]             # the newest stands for the run
    assert group["items"][0]["repeats"] == 3
    assert group["items"][0]["duplicate_ids"] == [second, first]
    # The rows are still waiting, so the backlog counts stay honest: collapsing
    # three repetitions into one card has not answered two of them.
    assert group["count"] == 3 and fresh_db.pending_count() == 3


def test_two_different_answers_to_one_person_both_stay(signed_in, fresh_db):
    first = held(fresh_db, MOM, "are you coming?", "on my way", 100)
    second = held(fresh_db, MOM, "and the keys?", "in my bag", 200)

    items = cards(signed_in)
    assert [i["id"] for i in items] == [second, first]
    assert [i["repeats"] for i in items] == [1, 1]
    assert [i["duplicate_ids"] for i in items] == [[], []]


def test_the_same_answer_waiting_for_two_people_is_never_collapsed(signed_in, fresh_db):
    """Collapsing is per conversation. "I'm fine, thanks" owed to two people is
    two messages that both have to go out."""
    mom = held(fresh_db, MOM, "you ok?", "I'm fine, thanks", 100)
    landlord = held(fresh_db, LANDLORD, "you ok?", "I'm fine, thanks", 200)

    r = signed_in.get("/api/held/by-contact").json()
    assert r["contacts"] == 2
    assert {g["chat_id"]: [i["id"] for i in g["items"]] for g in r["groups"]} == {MOM: [mom], LANDLORD: [landlord]}
    assert all(i["repeats"] == 1 for g in r["groups"] for i in g["items"])


def test_identical_drafts_with_a_different_one_between_them_still_collapse(signed_in, fresh_db):
    """An odd draft in between does not make two copies of one answer different."""
    older = held(fresh_db, MOM, "are you there?", "on my way", 100)
    other = held(fresh_db, MOM, "and the keys?", "in my bag", 200)
    newest = held(fresh_db, MOM, "hello?", "on my way", 300)

    items = cards(signed_in)
    assert [i["id"] for i in items] == [newest, other]
    assert items[0]["repeats"] == 2 and items[0]["duplicate_ids"] == [older]


def test_sending_the_kept_card_sends_one_message_and_clears_its_duplicates(signed_in, fresh_db, bridge):
    """The rows behind a collapsed card must not be left pending for ever, and
    one answer must leave as one WhatsApp message however many rows it stood for."""
    first = held(fresh_db, MOM, "are you there?", "on my way", 100)
    second = held(fresh_db, MOM, "hello?", "on my way", 200)
    third = held(fresh_db, MOM, "mum?", "on my way", 300)

    card = cards(signed_in)[0]
    r = signed_in.post(f"/api/held/{card['id']}/send", json={})
    assert r.status_code == 200 and r.json()["duplicates_dismissed"] == 2

    assert [j["text"] for j in bridge] == ["on my way"]
    assert fresh_db.get(third)["status"] == "sent"
    assert [fresh_db.get(i)["status"] for i in (second, first)] == ["dismissed", "dismissed"]
    assert fresh_db.pending() == []                                # nothing left stuck
    # A second click on the same card finds it handled and sends nothing more.
    assert signed_in.post(f"/api/held/{card['id']}/send", json={}).status_code == 409
    assert len(bridge) == 1


def test_an_edited_reply_still_resolves_the_rows_the_card_stood_for(signed_in, fresh_db, bridge):
    """The duplicates are the rows that said what the draft said, so editing
    the text before sending does not leave them behind."""
    first = held(fresh_db, MOM, "are you there?", "on my way", 100)
    second = held(fresh_db, MOM, "hello?", "on my way", 200)

    card = cards(signed_in)[0]
    r = signed_in.post(f"/api/held/{card['id']}/send", json={"text": "Niko njiani, dakika kumi."})
    assert r.json()["duplicates_dismissed"] == 1
    assert [j["text"] for j in bridge] == ["Niko njiani, dakika kumi."]
    assert fresh_db.get(second)["status"] == "sent" and fresh_db.get(first)["status"] == "dismissed"


def test_a_failed_send_leaves_the_whole_set_waiting(signed_in, fresh_db, monkeypatch):
    first = held(fresh_db, MOM, "are you there?", "on my way", 100)
    second = held(fresh_db, MOM, "hello?", "on my way", 200)

    def unreachable(url, json=None, headers=None, timeout=None):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(approvals.httpx, "post", unreachable)
    card = cards(signed_in)[0]
    assert signed_in.post(f"/api/held/{card['id']}/send", json={}).status_code == 502
    assert {r["id"] for r in fresh_db.pending()} == {first, second}


def test_dismissing_the_kept_card_dismisses_its_duplicates_too(signed_in, fresh_db):
    first = held(fresh_db, MOM, "are you there?", "on my way", 100)
    second = held(fresh_db, MOM, "hello?", "on my way", 200)
    other = held(fresh_db, MOM, "and the keys?", "in my bag", 300)

    card = [i for i in cards(signed_in) if i["id"] == second][0]
    r = signed_in.post(f"/api/held/{card['id']}/dismiss")
    assert r.status_code == 200 and r.json()["duplicates_dismissed"] == 1
    assert [fresh_db.get(i)["status"] for i in (second, first)] == ["dismissed", "dismissed"]
    assert [i["id"] for i in fresh_db.pending()] == [other]        # a different answer is untouched


# ---- who it is from ----

def test_a_privacy_id_whose_number_we_know_shows_the_number(signed_in, fresh_db):
    """The bug the owner reported as "it should not shoow the name a unknow":
    the bridge had resolved the number and the row was carrying it, while the
    card still said "Unknown contact"."""
    held(fresh_db, STRANGER, "who is this?", "hi", 100, number="254712345678")

    group = signed_in.get("/api/held/by-contact").json()["groups"][0]
    assert group["from"] == "+254712345678" and group["items"][0]["from"] == "+254712345678"
    assert signed_in.get("/api/held").json()["items"][0]["from"] == "+254712345678"


def test_a_privacy_id_with_no_number_at_all_still_says_something(signed_in, fresh_db):
    held(fresh_db, STRANGER, "who is this?", "hi", 100)
    assert signed_in.get("/api/held/by-contact").json()["groups"][0]["from"] == "Unknown contact"


def test_a_name_the_owner_saved_beats_the_name_the_sender_chose(signed_in, fresh_db):
    """A self-chosen display name never overrides the address book."""
    held(fresh_db, MOM, "are you coming?", "on my way", 100, push_name="MOMMY BOSS 254")

    group = signed_in.get("/api/held/by-contact").json()["groups"][0]
    assert group["name"] == "Mom" and group["from"] == "Mom"


def test_the_name_the_sender_chose_beats_a_bare_number(signed_in, fresh_db):
    held(fresh_db, STRANGER, "nikupigie?", "sawa", 100, number="254712345678", push_name="Kevo Mech")

    group = signed_in.get("/api/held/by-contact").json()["groups"][0]
    assert group["from"] == "Kevo Mech"
    # Still not a name the owner saved, so nothing claims it is one.
    assert group["name"] == ""


# ---- the context, short enough to read ----

def test_a_long_message_is_trimmed_for_the_card_and_kept_whole_beside_it(signed_in, fresh_db):
    long_text = "Habari yako boss, nilikuwa nataka kuuliza kuhusu ile kazi ya jana. " * 6
    held(fresh_db, MOM, long_text, "sawa", 100)

    card = cards(signed_in)[0]
    assert card["incoming"] == long_text                  # the full text is still there to expand
    assert len(card["incoming_short"]) <= 161             # 160 characters and the ellipsis
    assert card["incoming_short"].endswith("…")
    stem = card["incoming_short"][:-1]
    assert long_text.startswith(stem) and not stem.endswith(" ")   # cut on a word, no dangling space


def test_a_short_message_is_left_exactly_as_it_is(signed_in, fresh_db):
    held(fresh_db, MOM, "uko wapi?", "niko njiani", 100)
    assert cards(signed_in)[0]["incoming_short"] == "uko wapi?"


def test_an_arabic_message_is_cut_by_characters_not_bytes(signed_in, fresh_db):
    """Kiswahili, Sheng and Arabic all come through this queue. Cutting bytes
    would halve a character and leave the card showing a replacement glyph."""
    sentence = "هتيجي امتى يا حبيبي ده احنا مستنيين من الصبح " * 8
    held(fresh_db, MOM, sentence, "قريب", 100)

    short = cards(signed_in)[0]["incoming_short"]
    assert len(short) <= 161 and short.endswith("…")
    assert sentence.startswith(short[:-1]) and "�" not in short


def test_a_message_with_no_word_boundary_is_still_cut_to_fit(signed_in, fresh_db):
    """A long link has nowhere to break, and showing all of it is what made the
    card unreadable in the first place."""
    url = "https://example.com/" + "a" * 300
    held(fresh_db, MOM, url, "sawa", 100)

    short = cards(signed_in)[0]["incoming_short"]
    assert len(short) == 161 and short.endswith("…") and url.startswith(short[:-1])


def test_a_long_reason_is_trimmed_and_kept_whole_beside_it(signed_in, fresh_db):
    reason = "She asked for money and I could not tell which account she meant, so I left it. " * 3
    held(fresh_db, MOM, "tuma pesa", "nitatuma", 100, why=reason)

    card = cards(signed_in)[0]
    assert card["why"] == reason
    assert len(card["why_short"]) <= 121 and card["why_short"].endswith("…")


def test_a_one_line_reason_is_left_exactly_as_it_is(signed_in, fresh_db):
    held(fresh_db, MOM, "tuma pesa", "nitatuma", 100, why="Money, and I am not sure which account.")

    card = cards(signed_in)[0]
    assert card["why_short"] == card["why"] == "Money, and I am not sure which account."
