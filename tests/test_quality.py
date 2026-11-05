"""Does the quality report tell the owner the truth?

Every test runs on a fixed clock and seeded rows: a report that reads the
system clock would pass or fail depending on the hour it was run, and a report
about whether she is improving must not be one of those.
"""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import dashboard, quality

# Half past two in the afternoon, so "today" has a before and an after inside it.
NOW = datetime(2026, 11, 5, 14, 30, tzinfo=quality.LOCAL_TZ)
CHAT = "254700000000@c.us"
# Text that must never appear in a response or a query.
INCOMING = "unaweza kunitumia hiyo pesa leo?"
DRAFT = "Nitakuambia."


def at(day: int, hour: int = 12, minute: int = 0, second: int = 0, month: int = 11) -> float:
    return datetime(2026, month, day, hour, minute, second, tzinfo=quality.LOCAL_TZ).timestamp()


def seed_incoming(store, ts: float, outcome: str) -> None:
    """One message that reached her, and what became of it."""
    with store._db() as c:
        c.execute("INSERT INTO message_log (ts, chat_id, name, direction, body, outcome) "
                  "VALUES (?, ?, ?, ?, ?, ?)", (ts, CHAT, "Mom", "in", INCOMING, outcome))


def seed_draft(store, ts: float, status: str, reason: str = "needs_owner") -> None:
    with store._db() as c:
        c.execute("INSERT INTO held (ts, chat_id, incoming, reply, status, reason) "
                  "VALUES (?, ?, ?, ?, ?, ?)", (ts, CHAT, INCOMING, DRAFT, status, reason))


def seed_drafts(store, ts: float, status: str, count: int, reason: str = "needs_owner") -> None:
    for _ in range(count):
        seed_draft(store, ts, status, reason)


@pytest.fixture()
def quality_client(fresh_db, contacts_file):
    """The quality router wired the way app/main.py wires the others, plus the
    dashboard's own router so a test can sign in."""
    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(quality.router)
    dashboard._failed_logins.clear()
    return TestClient(app)


def test_the_counts_match_the_rows_that_were_seeded(fresh_db):
    for _ in range(3):
        seed_incoming(fresh_db, at(5, 9), "replied")
    seed_incoming(fresh_db, at(5, 10), "held")
    seed_draft(fresh_db, at(5, 10), "pending")
    seed_draft(fresh_db, at(5, 11), "sent")
    seed_draft(fresh_db, at(5, 11), "dismissed")

    body = quality.report("today", NOW)
    assert body["messages"]["arrived"] == 4
    assert body["messages"]["answered_herself"] == 3
    assert body["messages"]["held_for_you"] == 1
    assert body["messages"]["other"] == 0
    assert body["drafts"]["written"] == 3
    assert body["drafts"]["you_sent"] == 1
    assert body["drafts"]["you_dismissed"] == 1
    assert body["drafts"]["decided"] == 2
    assert body["drafts"]["still_waiting"] == 1


def test_a_draft_the_owner_is_mid_send_on_counts_as_sent_not_as_waiting(fresh_db):
    """'sending' is the moment between the click and the bridge's answer. The
    owner has already decided; calling it a backlog would blame him for it."""
    seed_draft(fresh_db, at(5, 11), "sending")
    drafts = quality.report("today", NOW)["drafts"]
    assert (drafts["you_sent"], drafts["still_waiting"]) == (1, 0)


def test_a_period_begins_at_local_midnight(fresh_db):
    """The rule app/controls.py's time_bounds uses, so the two agree about
    which hours "today" means."""
    seed_draft(fresh_db, at(4, 23, 59, 59), "sent")       # yesterday, one second before
    seed_draft(fresh_db, at(5, 0, 0, 0), "sent")          # today, on the stroke

    assert quality.report("today", NOW)["drafts"]["written"] == 1
    assert quality.report("yesterday", NOW)["drafts"]["written"] == 1
    assert quality.report("last_7_days", NOW)["drafts"]["written"] == 2


def test_seven_days_reaches_back_six_midnights_and_no_further(fresh_db):
    seed_draft(fresh_db, at(30, 12, month=10), "sent")            # 30 Oct: the earliest day in
    seed_draft(fresh_db, at(29, 23, 59, 59, month=10), "sent")    # 29 Oct: one second too early
    assert quality.report("last_7_days", NOW)["drafts"]["written"] == 1


def test_a_rate_is_refused_below_the_threshold_and_the_counts_given_instead(fresh_db):
    seed_drafts(fresh_db, at(5, 9), "sent", 7)
    seed_drafts(fresh_db, at(5, 9), "dismissed", 2)

    drafts = quality.report("today", NOW)["drafts"]
    assert drafts["decided"] == 9
    assert drafts["sent_share"] is None and drafts["enough_to_rate"] is False
    assert "%" not in drafts["sentence"]
    assert "7" in drafts["sentence"] and str(quality.MIN_DECISIONS_FOR_A_RATE) in drafts["sentence"]


def test_a_rate_appears_once_there_are_enough_decisions(fresh_db):
    seed_drafts(fresh_db, at(5, 9), "sent", 18)
    seed_drafts(fresh_db, at(5, 9), "dismissed", 2)

    drafts = quality.report("today", NOW)["drafts"]
    assert drafts["decided"] == 20 and drafts["enough_to_rate"] is True
    assert drafts["sent_share"] == 90.0
    assert "90%" in drafts["sentence"]


def test_hold_reasons_come_back_in_words_with_no_raw_keys(fresh_db):
    seed_drafts(fresh_db, at(5, 9), "pending", 2, reason="not_grounded")
    seed_draft(fresh_db, at(5, 10), "pending", reason="needs_owner")
    seed_draft(fresh_db, at(5, 11), "pending", reason="quiet_hours")

    holds = quality.report("today", NOW)["holds"]
    assert [r["reason"] for r in holds["reasons"]] == [
        "May contain a fact it wasn't sure about", "Needs you to answer",
        "Quiet hours — written, but not sent at this time of night"]
    assert [r["count"] for r in holds["reasons"]] == [2, 1, 1]
    for raw in ("not_grounded", "needs_owner", "quiet_hours", "_"):
        assert raw not in json.dumps(holds, ensure_ascii=False)
    assert "May contain a fact it wasn't sure about (2 of 4)" in holds["sentence"]


def test_a_groups_hold_reason_is_passed_through_as_the_sentence_it_already_is(fresh_db):
    """app/main.py stores groups.Decision.reason, which is the phrase and not a
    code, so this report must not try to translate it."""
    seed_draft(fresh_db, at(5, 9), "pending",
               reason="This group is set to ask you before anything goes out")
    reasons = quality.report("today", NOW)["holds"]["reasons"]
    assert reasons == [{"reason": "This group is set to ask you before anything goes out",
                        "count": 1}]


def test_a_reason_this_report_has_never_met_is_still_readable(fresh_db):
    seed_draft(fresh_db, at(5, 9), "pending", reason="some_new_gate")
    assert quality.report("today", NOW)["holds"]["reasons"][0]["reason"] == "Some new gate"


def test_a_draft_held_with_no_reason_recorded_says_so(fresh_db):
    seed_draft(fresh_db, at(5, 9), "pending", reason="")
    assert quality.report("today", NOW)["holds"]["reasons"][0]["reason"] == "No reason was recorded"


def test_the_trend_compares_the_previous_period_of_the_same_length(fresh_db):
    seed_drafts(fresh_db, at(5, 9), "sent", 18)           # today: 90%
    seed_drafts(fresh_db, at(5, 9), "dismissed", 2)
    seed_drafts(fresh_db, at(4, 9), "sent", 14)           # yesterday: 70%
    seed_drafts(fresh_db, at(4, 9), "dismissed", 6)
    seed_drafts(fresh_db, at(3, 9), "dismissed", 50)      # two days back: out of both

    trend = quality.report("today", NOW)["trend"]
    assert trend["previous"]["decided"] == 20
    assert trend["previous"]["sent_share"] == 70.0
    assert trend["previous"]["since"] == at(4, 0) and trend["previous"]["until"] == at(5, 0)
    assert trend["change"] == 20.0 and trend["direction"] == "better"
    assert "90%" in trend["sentence"] and "70%" in trend["sentence"]


def test_a_week_is_compared_against_the_week_before_it(fresh_db):
    """Not against the previous day: the period before a period is the same
    number of days, or the two numbers are not comparable at all."""
    trend = quality.report("last_7_days", NOW)["trend"]
    assert trend["previous"]["since"] == at(23, 0, month=10)        # 23 Oct midnight
    assert trend["previous"]["until"] == at(30, 0, month=10)        # where this week starts
    assert trend["previous"]["until"] == quality.period_bounds("last_7_days", NOW)[0]


def test_the_trend_refuses_to_judge_two_small_samples(fresh_db):
    seed_drafts(fresh_db, at(5, 9), "sent", 3)
    seed_drafts(fresh_db, at(4, 9), "sent", 2)

    trend = quality.report("today", NOW)["trend"]
    assert trend["direction"] == "unknown" and trend["change"] is None
    assert "3" in trend["sentence"] and "2" in trend["sentence"]


def test_a_difference_smaller_than_one_draft_is_called_level(fresh_db):
    """20 of 20 against 39 of 40 is one point apart, and one draft in the
    smaller sample is worth five. That gap came from the sample sizes."""
    seed_drafts(fresh_db, at(5, 9), "sent", 20)
    seed_drafts(fresh_db, at(4, 9), "sent", 39)
    seed_draft(fresh_db, at(4, 9), "dismissed")

    trend = quality.report("today", NOW)["trend"]
    assert trend["direction"] == "level" and "about the same" in trend["sentence"]


def test_an_empty_period_reports_nothing_rather_than_dividing_by_zero(fresh_db):
    body = quality.report("yesterday", NOW)
    assert body["messages"]["arrived"] == 0
    assert body["drafts"]["written"] == 0 and body["drafts"]["sent_share"] is None
    assert body["drafts"]["decided"] == 0
    assert body["holds"]["reasons"] == []
    assert body["queue"] == {"waiting": 0, "waiting_under_an_hour": 0, "waiting_today": 0,
                             "waiting_over_a_day": 0, "oldest_waiting_seconds": 0,
                             "sentence": "Nothing is waiting for you."}
    assert body["trend"]["direction"] == "unknown"
    for section in ("window", "messages", "drafts", "holds", "queue", "trend"):
        assert body[section]["sentence"].strip()


def test_the_queue_is_the_whole_backlog_and_says_how_long_it_has_waited(fresh_db):
    """Not bounded by the period: a draft from last week still waiting is this
    minute's problem, and a report of "today" that hid it would let it rot."""
    seed_draft(fresh_db, NOW.timestamp() - 600, "pending")             # 10 minutes
    seed_draft(fresh_db, NOW.timestamp() - 5 * 3600, "pending")        # 5 hours
    seed_draft(fresh_db, at(1, 9), "pending")                          # four days back
    seed_draft(fresh_db, at(1, 9), "sent")                             # decided: not waiting

    queue = quality.report("today", NOW)["queue"]
    assert queue["waiting"] == 3
    assert queue["waiting_under_an_hour"] == 1
    assert queue["waiting_today"] == 1
    assert queue["waiting_over_a_day"] == 1
    assert queue["oldest_waiting_seconds"] == round(NOW.timestamp() - at(1, 9))
    assert "4 days" in queue["sentence"] and "more than a day" in queue["sentence"]


def test_one_waiting_draft_is_described_in_the_singular(fresh_db):
    """And without repeating itself: the oldest draft of one is the only one,
    so a second clause about it being a day old would say nothing new."""
    seed_draft(fresh_db, at(1, 9), "pending")
    assert (quality.report("today", NOW)["queue"]["sentence"]
            == "1 draft is waiting for you. The oldest has been there 4 days.")


def test_a_queue_that_is_overnight_all_the_way_through_says_so(fresh_db):
    seed_drafts(fresh_db, at(1, 9), "pending", 3)
    sentence = quality.report("today", NOW)["queue"]["sentence"]
    assert sentence.startswith("3 drafts are waiting for you.")
    assert "All 3 have been there more than a day." in sentence


def test_the_window_reports_the_real_retention(fresh_db, monkeypatch):
    monkeypatch.setenv("MESSAGE_RETENTION_DAYS", "7")
    today = quality.report("today", NOW)["window"]
    assert today["retention_days"] == 7 and today["complete"] is True
    assert "7 days" in today["sentence"]

    month = quality.report("last_30_days", NOW)["window"]
    assert month["days"] == 30 and month["retention_days"] == 7
    assert month["complete"] is False and "purged" in month["sentence"]
    assert month["since"] == at(7, 0, month=10)  # 30 days back from tomorrow midnight


def test_thirty_days_is_whole_under_the_default_retention(fresh_db, monkeypatch):
    monkeypatch.setenv("MESSAGE_RETENTION_DAYS", "30")
    window = quality.report("last_30_days", NOW)["window"]
    assert window["retention_days"] == 30 and window["complete"] is True


def test_the_route_requires_signing_in(quality_client):
    assert quality_client.get("/api/quality").status_code == 401
    assert quality_client.get("/api/quality?period=last_7_days").status_code == 401


def test_the_route_answers_a_known_period_and_refuses_an_invented_one(quality_client, fresh_db):
    assert quality_client.post("/api/login", json={"password": "test-password"}).status_code == 200
    seed_draft(fresh_db, datetime.now(quality.LOCAL_TZ).timestamp(), "pending")

    r = quality_client.get("/api/quality?period=last_7_days")
    assert r.status_code == 200
    body = r.json()
    assert body["period"] == "last_7_days" and body["drafts"]["written"] == 1
    assert quality_client.get("/api/quality").json()["period"] == "today"
    assert quality_client.get("/api/quality?period=forever").status_code == 422


def test_no_message_text_reaches_the_response(fresh_db):
    seed_incoming(fresh_db, at(5, 9), "held")
    seed_draft(fresh_db, at(5, 9), "pending")
    printed = json.dumps(quality.report("today", NOW), ensure_ascii=False)
    assert INCOMING not in printed and DRAFT not in printed


@pytest.fixture()
def statements(monkeypatch):
    """Every SQL statement the report runs, in order."""
    seen: list[str] = []
    real_connect = sqlite3.connect

    def traced(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced)
    return seen


def selects(statements: list[str]) -> list[str]:
    return [s for s in statements if s.lstrip().upper().startswith("SELECT")]


def test_every_number_is_counted_in_sql_and_no_rows_are_read(fresh_db, statements):
    seed_incoming(fresh_db, at(5, 9), "replied")
    seed_draft(fresh_db, at(5, 9), "pending")
    statements.clear()

    quality.report("today", NOW)
    queried = selects(statements)
    assert queried, "the report ran no queries at all"
    for sql in queried:
        assert any(fn in sql.upper() for fn in ("COUNT(", "SUM(", "MIN(")), sql
        # The text columns are never named, so no row of anybody's words can
        # be fetched here even by accident.
        for column in ("body", "incoming", "reply", "push_name", "name"):
            assert column not in sql.lower(), sql


def test_the_report_costs_the_same_number_of_queries_as_the_log_grows(fresh_db, statements):
    seed_drafts(fresh_db, at(5, 9), "sent", 3)
    statements.clear()
    quality.report("today", NOW)
    small = len(selects(statements))

    seed_drafts(fresh_db, at(5, 10), "sent", 200)
    for _ in range(200):
        seed_incoming(fresh_db, at(5, 10), "replied")
    statements.clear()
    quality.report("today", NOW)

    assert len(selects(statements)) == small


def test_a_span_of_time_is_spelled_the_way_a_person_would_say_it():
    assert quality.spell_duration(0) == "less than a minute"
    assert quality.spell_duration(59) == "less than a minute"
    assert quality.spell_duration(60) == "a minute"
    assert quality.spell_duration(600) == "10 minutes"
    assert quality.spell_duration(3600) == "an hour"
    assert quality.spell_duration(2 * 3600) == "2 hours"
    assert quality.spell_duration(86400) == "a day"
    assert quality.spell_duration(3 * 86400) == "3 days"


def test_the_bounds_of_every_period_are_whole_local_days():
    for period, (days, _) in quality.PERIODS.items():
        since, until = quality.period_bounds(period, NOW)
        assert until > since
        start = datetime.fromtimestamp(since, quality.LOCAL_TZ)
        assert (start.hour, start.minute, start.second) == (0, 0, 0)
        assert datetime.fromtimestamp(until, quality.LOCAL_TZ) - start == timedelta(days=days)
