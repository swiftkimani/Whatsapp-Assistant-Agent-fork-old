"""Is she working, and is she getting better or worse?

One read-only report over what is already recorded: `message_log`'s outcomes,
and the held queue's statuses and reasons. Nothing new is stored.

Three rules shape everything below.

Counted in SQL. This is polled by an open dashboard, so every number here is a
COUNT/SUM/MIN over an indexed range; no query fetches rows for Python to count,
and none gets slower as the log grows.

Honest with little data. A percentage over four drafts is noise wearing a
percent sign. Below MIN_DECISIONS_FOR_A_RATE the counts are given and the ratio
is refused — see the comment there.

No message text. Counts, spans of time, and the owner-readable sentence that
goes with them. Nothing in the response, and nothing logged: a poll every few
seconds has no business writing a log line, and this report has no business
carrying anybody's words.
"""
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query

from app import db
from app.controls import LOCAL_TZ   # one definition of the owner's timezone
from app.dashboard import require_auth

router = APIRouter()
AUTH = [Depends(require_auth)]

HOUR_SECONDS = 3600
DAY_SECONDS = 86400

# (whole days, the words that finish "Counted …" and "came in …").
PERIODS = {
    "today": (1, "today"),
    "yesterday": (1, "yesterday"),
    "last_7_days": (7, "over the last 7 days"),
    "last_30_days": (30, "over the last 30 days"),
}
Period = Literal["today", "yesterday", "last_7_days", "last_30_days"]

# How many decisions it takes before a percentage is worth printing.
#
# A share is only worth reading if one more decision cannot move it further
# than the owner would act on. At twenty decisions one draft is five points; at
# ten it is ten, and at four it is twenty-five — a number that swings by a
# quarter on a single click says nothing about her and everything about the
# sample. Twenty is a floor and not a guarantee: the 95% interval on a
# half-and-half split at twenty is still about ±22 points, which is why the
# rate is never shown alone — always beside its own counts and beside the
# period before it, so it reads as a direction rather than a measurement.
#
# Below the floor nothing is hidden: the owner is told "7 of 9", which is the
# honest version of the same fact.
MIN_DECISIONS_FOR_A_RATE = 20

# Why she held, in the owner's words. The same wording as the Held cards in
# app/dashboard.html, which is where these phrases are agreed.
HOLD_REASONS = {
    "needs_owner": "Needs you to answer",
    "not_grounded": "May contain a fact it wasn't sure about",
    "empty_reply": "The model gave no usable answer",
    "paused": "Auto-reply was paused",
    "contact_mode": "This contact is set to always ask you first",
    "quiet_hours": "Quiet hours — written, but not sent at this time of night",
    "": "No reason was recorded",
}


def readable_reason(stored: str) -> str:
    """One hold reason as a person reads it.

    A group's hold already arrives as a sentence rather than a key: app/main.py
    stores groups.Decision.reason, which is the phrase. So anything with a
    space in it is passed through as written, exactly as the Held card does it.
    A key this file has not met is turned into words rather than printed raw —
    a reason the owner cannot read is worse than no reason at all.
    """
    phrase = HOLD_REASONS.get(stored)
    if phrase is not None:
        return phrase
    text = (stored or "").strip()
    if " " in text:
        return text
    words = text.replace("_", " ")
    return words[0].upper() + words[1:] if words else HOLD_REASONS[""]


def spell_duration(seconds: float) -> str:
    """A span of time in words, rounded down to its largest whole unit."""
    whole = max(0, int(seconds))
    for size, one, many in ((DAY_SECONDS, "a day", "days"),
                            (HOUR_SECONDS, "an hour", "hours"),
                            (60, "a minute", "minutes")):
        if whole >= size:
            count = whole // size
            return one if count == 1 else f"{count} {many}"
    return "less than a minute"


def _things(count: int, noun: str) -> str:
    """"1 draft" / "3 drafts" — the plural every sentence below needs."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def period_bounds(period: str, now: datetime | None = None) -> tuple[float, float]:
    """(since, until) for a period, as timestamps.

    Local midnight starts a day — the rule app/controls.py's time_bounds uses —
    so "today" here and "today" in the message list mean the same hours. Unlike
    time_bounds, both ends are always concrete: a period with an open end has
    no length, and without a length there is no period of the same length
    before it to compare against. An `until` a few hours in the future is
    harmless, because no row can be logged there.
    """
    days, _ = PERIODS.get(period, PERIODS["today"])
    now = now or datetime.now(LOCAL_TZ)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = midnight if period == "yesterday" else midnight + timedelta(days=1)
    return (end - timedelta(days=days)).timestamp(), end.timestamp()


def _days_earlier(ts: float, days: int) -> float:
    """`ts` moved back whole local days, so the period before a period starts
    at the same hour of the day it did."""
    return (datetime.fromtimestamp(ts, LOCAL_TZ) - timedelta(days=days)).timestamp()


# ---- the queries ----
# Each takes an open connection: one report is one connection, not one per
# number. db._db() rather than a connection of this module's own because the
# WAL mode, the busy timeout and the row factory are app/db.py's single
# decision, and a second copy of them here would be a second answer to
# "database is locked".


def _incoming_outcomes(c, since: float, until: float) -> dict[str, int]:
    """How many messages came in, by what became of each.

    Only messages the assistant handled are in `message_log` at all: one from
    somebody on "leave alone" is dropped before anything is stored. So this
    counts what reached her, not everything the phone received.
    """
    rows = c.execute(
        "SELECT outcome, COUNT(*) AS n FROM message_log "
        "WHERE direction = 'in' AND ts >= ? AND ts < ? GROUP BY outcome",
        (since, until))
    return {(r["outcome"] or ""): r["n"] for r in rows}


def _draft_statuses(c, since: float, until: float) -> dict[str, int]:
    """The drafts she wrote in this period, by where each one ended up.

    By when the draft was written, because that is the only time the row
    carries: `held` has `ts` and no record of when the owner decided. A draft
    written at the end of a period and sent the next morning is counted in the
    period it was written in, and _drafts() says so in words.
    """
    rows = c.execute(
        "SELECT status, COUNT(*) AS n FROM held WHERE ts >= ? AND ts < ? GROUP BY status",
        (since, until))
    return {(r["status"] or ""): r["n"] for r in rows}


def _hold_reasons(c, since: float, until: float) -> list[tuple[str, int]]:
    rows = c.execute(
        "SELECT reason, COUNT(*) AS n FROM held WHERE ts >= ? AND ts < ? "
        "GROUP BY reason ORDER BY n DESC, reason",
        (since, until))
    return [(r["reason"] or "", r["n"]) for r in rows]


def _overnight_clause(waiting: int, over_a_day: int) -> str:
    """How much of the queue has been there since yesterday, or nothing.

    A queue he never clears is a different problem from one he clears late, so
    this is the half of the queue sentence that distinguishes them. One draft
    that is both the only one waiting and the oldest needs no clause: the
    sentence before it has already said how long it has been there.
    """
    if not over_a_day:
        return ""
    if over_a_day == waiting:
        return "" if waiting == 1 else f" All {waiting} have been there more than a day."
    verb = "has" if over_a_day == 1 else "have"
    return f" {over_a_day} of them {verb} been there more than a day."


def _waiting_queue(c, now_ts: float) -> dict:
    """The queue as it stands, not as this period left it.

    Deliberately not bounded by the period: a draft from last week still
    waiting is this minute's problem, and a report of "today" that hid it
    would be the report that lets the queue grow.
    """
    row = c.execute(
        "SELECT COUNT(*) AS waiting, MIN(ts) AS oldest, "
        "SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS under_an_hour, "
        "SUM(CASE WHEN ts < ? THEN 1 ELSE 0 END) AS over_a_day "
        "FROM held WHERE status = 'pending'",
        (now_ts - HOUR_SECONDS, now_ts - DAY_SECONDS)).fetchone()
    waiting = row["waiting"] or 0
    under_an_hour, over_a_day = row["under_an_hour"] or 0, row["over_a_day"] or 0
    oldest = max(0.0, now_ts - row["oldest"]) if waiting else 0.0
    report = {"waiting": waiting, "waiting_under_an_hour": under_an_hour,
              # Everything between the two: waiting a while, but not overnight.
              "waiting_today": waiting - under_an_hour - over_a_day,
              "waiting_over_a_day": over_a_day,
              "oldest_waiting_seconds": round(oldest)}
    if not waiting:
        report["sentence"] = "Nothing is waiting for you."
    else:
        verb = "is" if waiting == 1 else "are"
        report["sentence"] = (
            f"{_things(waiting, 'draft')} {verb} waiting for you. The oldest has been "
            f"there {spell_duration(oldest)}.{_overnight_clause(waiting, over_a_day)}")
    return report


# ---- the numbers, and the sentence that goes with each ----


def _messages(outcomes: dict[str, int], label: str) -> dict:
    answered, held = outcomes.get("replied", 0), outcomes.get("held", 0)
    arrived = sum(outcomes.values())
    if not arrived:
        sentence = f"No messages came in {label} from people she is set to handle."
    else:
        sentence = (f"{_things(arrived, 'message')} came in {label} from people she is "
                    f"set to handle. She answered {answered} herself and held {held} for you.")
    return {"arrived": arrived, "answered_herself": answered, "held_for_you": held,
            # Whatever the log holds that this report has no name for, so the
            # three numbers above can always be checked against the total.
            "other": arrived - answered - held, "sentence": sentence}


def _decisions(statuses: dict[str, int]) -> dict:
    """The counts behind the drafts section, without any wording.

    A row in 'sending' is one the owner has already pressed send on; it becomes
    'sent' a moment later, and counting it as still waiting would make their
    own click look like a backlog.
    """
    written = sum(statuses.values())
    sent = statuses.get("sent", 0) + statuses.get("sending", 0)
    dismissed = statuses.get("dismissed", 0)
    decided = sent + dismissed
    enough = decided >= MIN_DECISIONS_FOR_A_RATE
    return {"written": written, "you_sent": sent, "you_dismissed": dismissed,
            "decided": decided, "still_waiting": written - decided,
            "sent_share": round(100 * sent / decided, 1) if enough else None,
            "enough_to_rate": enough}


def _drafts(counts: dict, label: str) -> dict:
    decided, sent, dismissed = counts["decided"], counts["you_sent"], counts["you_dismissed"]
    if not counts["written"]:
        sentence = f"She wrote no drafts {label}."
    elif not decided:
        sentence = (f"She wrote {_things(counts['written'], 'draft')} {label} and you have "
                    "not decided on any of them yet.")
    elif counts["sent_share"] is None:
        sentence = (f"You decided on {_things(decided, 'draft')} she wrote {label}: sent "
                    f"{sent}, dismissed {dismissed}. Too few to put a percentage on — that "
                    f"takes {MIN_DECISIONS_FOR_A_RATE} decisions.")
    else:
        sentence = (f"You sent {sent} of the {decided} drafts you decided on "
                    f"({counts['sent_share']:g}%) and dismissed {dismissed}. Counted by when "
                    "she wrote them, because that is the only time the queue records.")
    return {**counts, "sentence": sentence}


def _holds(reasons: list[tuple[str, int]], written: int) -> dict:
    listed = [{"reason": readable_reason(key), "count": n} for key, n in reasons]
    if not listed:
        sentence = "Nothing was held."
    elif len(listed) == 1:
        sentence = (f"Held because: {listed[0]['reason']}." if written == 1
                    else f"All {written} were held because: {listed[0]['reason']}.")
    else:
        top = listed[0]
        sentence = f"Most often: {top['reason']} ({top['count']} of {written})."
    return {"reasons": listed, "sentence": sentence}


def _trend(current: dict, previous: dict, label: str) -> dict:
    """This period's sent share against the period before it, same length.

    "Level" is not |change| < 0 — it is a change smaller than one draft is
    worth in the smaller of the two samples. A difference that one decision
    could not have produced came from the sample sizes, not from her.
    """
    body = {"previous": previous, "change": None, "direction": "unknown"}
    now_share, was_share = current["sent_share"], previous["sent_share"]
    if now_share is None or was_share is None:
        body["sentence"] = (
            f"Not enough decisions to compare yet: {current['decided']} {label}, "
            f"{previous['decided']} the period before, and a rate takes "
            f"{MIN_DECISIONS_FOR_A_RATE}.")
        return body

    change = round(now_share - was_share, 1)
    one_draft = 100 / min(current["decided"], previous["decided"])
    body["change"] = change
    against = f"You sent {now_share:g}% of her drafts {label}, against {was_share:g}% the period before"
    if abs(change) < one_draft:
        body["direction"] = "level"
        body["sentence"] = f"{against} — about the same."
    elif change > 0:
        body["direction"] = "better"
        body["sentence"] = f"{against} — she is getting closer to what you would send yourself."
    else:
        body["direction"] = "worse"
        body["sentence"] = f"{against} — she is drifting from what you would send yourself."
    return body


def _window(since: float, until: float, days: int, label: str, now_ts: float) -> dict:
    """What this report really covers, so the page cannot imply more history
    than exists. `message_log` is purged after MESSAGE_RETENTION_DAYS, so "all
    time" is a lie past that and is not on offer as a period."""
    kept = db.retention_days()
    complete = since >= now_ts - kept * DAY_SECONDS
    sentence = f"Counted {label}. The message log keeps {_things(kept, 'day')}"
    sentence += (", and this period sits inside that." if complete else
                 ", so the start of this period has already been purged and the message "
                 "counts begin where the log does. Her drafts are not purged, so the draft "
                 "counts are whole.")
    return {"since": since, "until": until, "days": days, "retention_days": kept,
            "complete": complete, "sentence": sentence}


def report(period: str, now: datetime | None = None) -> dict:
    """Everything the owner needs to judge her, for one period."""
    days, label = PERIODS.get(period, PERIODS["today"])
    since, until = period_bounds(period, now)
    now_ts = (now or datetime.now(LOCAL_TZ)).timestamp()
    was_since = _days_earlier(since, days)

    with db._db() as c:
        outcomes = _incoming_outcomes(c, since, until)
        statuses = _draft_statuses(c, since, until)
        reasons = _hold_reasons(c, since, until)
        queue = _waiting_queue(c, now_ts)
        before = _draft_statuses(c, was_since, since)

    counts = _decisions(statuses)
    previous = {**_decisions(before), "since": was_since, "until": since}
    return {"period": period if period in PERIODS else "today", "label": label,
            "generated_at": now_ts,
            "window": _window(since, until, days, label, now_ts),
            "messages": _messages(outcomes, label),
            "drafts": _drafts(counts, label),
            "holds": _holds(reasons, counts["written"]),
            "queue": queue,
            "trend": _trend(counts, previous, label)}


@router.get("/api/quality", dependencies=AUTH)
def quality(period: Period = Query("today")):
    return report(period)
