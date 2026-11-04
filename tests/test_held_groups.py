"""The held queue grouped by the person it is waiting on.

Fifty drafts in a flat list is not a backlog anyone works through, so these
cover the grouped view and the two capped counts that made a real backlog
invisible: a Held badge that stopped at fifty and a query that could not see
past the fiftieth row.
"""
MOM = "201000000000@c.us"            # in the test contacts.json
LULU = "112257593794569@lid"         # also in it, as a privacy id
LANDLORD = "254712345678@c.us"       # in nobody's contacts


def held(store, chat_id, incoming, reply, ts):
    """One waiting draft whose arrival time is set, so the ordering these tests
    assert is the stored time and not a guess about how fast the inserts ran."""
    item = store.add_held(chat_id, incoming, reply)
    with store._db() as c:
        c.execute("UPDATE held SET ts = ? WHERE id = ?", (ts, item))
    return item


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
