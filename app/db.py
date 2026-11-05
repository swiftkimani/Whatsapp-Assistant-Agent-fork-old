"""SQLite store (data/assistant.db): held replies, audit log, conversation
memory, per-contact modes, per-chat directions and settings."""
import os
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

from app.log import log_event

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "assistant.db"
_last_purge = 0.0


@contextmanager
def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Several people can be mid-conversation at once: the webhook runs the
    # agent in a threadpool, so replies, memory writes and the message log
    # overlap. On SQLite's defaults the second writer gets "database is
    # locked" after five seconds and that person's reply is simply lost.
    #
    # WAL lets readers carry on while one writer commits, and the busy
    # timeout makes a writer wait its turn instead of failing. Both are
    # per-connection, so they are set here rather than once at startup.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _add_column(c, table: str, column: str, ddl: str) -> None:
    """Tiny migration helper so an existing assistant.db keeps working."""
    if column not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init() -> None:
    with _db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS held (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, chat_id TEXT,
            incoming TEXT, reply TEXT, status TEXT DEFAULT 'pending')""")
        _add_column(c, "held", "reason", "TEXT DEFAULT ''")
        _add_column(c, "held", "number", "TEXT DEFAULT ''")
        # The model's own one-line account of why it held, for the owner to read.
        _add_column(c, "held", "why", "TEXT DEFAULT ''")
        # The display name the sender chose for themselves, as WhatsApp reports
        # it. Only ever read back as text: it is a stranger's own words, so it
        # is stored and used as a bound parameter and never built into SQL.
        _add_column(c, "held", "push_name", "TEXT DEFAULT ''")
        # The WhatsApp id of the message this draft answers, so a reply the
        # owner approves can quote it the way an automatic one does. Without
        # it an approved draft could never show what it was answering, which
        # is the whole point of approving it hours later.
        _add_column(c, "held", "msg_id", "TEXT DEFAULT ''")
        # Whether the owner changed the draft before sending it. This is the
        # number that says whether she is any good: sending untouched and
        # rewriting first both count as "sent", and only this tells them
        # apart. Recorded at send time -- what was actually sent is not kept
        # anywhere else on the row.
        _add_column(c, "held", "was_edited", "INTEGER DEFAULT 0")
        # Sent and dismissed rows are kept for ever, so the queue's own reads —
        # which run on every dashboard poll and ask only for the pending ones,
        # sometimes in one conversation — would scan a table that only grows.
        c.execute("CREATE INDEX IF NOT EXISTS idx_held_pending ON held (status, chat_id, id)")
        # The quality report counts drafts over a period, and held is the one
        # table here that is never purged -- so without this those two queries
        # scan the whole of it and get slower every week. Measured at 50k
        # rows: 8.7ms to 1.6ms for a day's report.
        c.execute("CREATE INDEX IF NOT EXISTS idx_held_ts ON held (ts)")
        c.execute("""CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, target TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, chat_id TEXT, role TEXT, content TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id, id)")
        c.execute("CREATE TABLE IF NOT EXISTS contact_modes (name TEXT PRIMARY KEY, mode TEXT NOT NULL)")
        # One row per group the owner has decided about. Keyed on the chat id
        # and not on a name, because a group has no contacts.json entry to
        # hang a name on: contact_modes keys on the name, which is why it
        # cannot hold these. Two columns because there are two questions —
        # when she may answer at all, and whether the answer goes out by
        # itself — and conflating them would mean a group could not be set to
        # "only when somebody is talking to her, and show me first".
        c.execute("""CREATE TABLE IF NOT EXISTS group_modes (
            chat_id TEXT PRIMARY KEY, mode TEXT NOT NULL, delivery TEXT NOT NULL)""")
        # One row per conversation the owner has steered: the short standing
        # instruction, in his own words, that every reply in that chat is then
        # written under. See the block above set_direction() for why neither
        # settings nor contact_modes can hold it.
        c.execute("""CREATE TABLE IF NOT EXISTS chat_directions (
            chat_id TEXT PRIMARY KEY, direction TEXT NOT NULL, set_at REAL NOT NULL)""")
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute("""CREATE TABLE IF NOT EXISTS message_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, chat_id TEXT, name TEXT,
            direction TEXT, body TEXT, outcome TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_message_log_ts ON message_log (ts)")
        indexed = _add_message_search(c)
    if indexed:
        # Said out loud because it happens once, on the first boot after this
        # shipped, and it is the one boot that does real work before serving:
        # a month of messages read and indexed. A count, never a body.
        log_event("message_search_backfilled", messages=indexed)
    purge_messages()


# ---- held replies ----

def add_held(chat_id: str, incoming: str, reply: str, reason: str = "",
             number: str | None = None, why: str = "", push_name: str = "",
             msg_id: str = "") -> int:
    with _db() as c:
        cur = c.execute(
            "INSERT INTO held (ts, chat_id, incoming, reply, reason, number, why, push_name, msg_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), chat_id, incoming, reply, reason, number or "", why,
             push_name or "", msg_id or ""))
        return cur.lastrowid


def pending(limit: int = 50) -> list[dict]:
    """The replies waiting for the owner, newest first.

    The cap was a hard 50 written into the SQL. That suits a page of cards and
    defeats anything that wants to see a real backlog whole — grouping fifty
    drafts by person cannot find the fifty-first — so the caller chooses, within
    the same bound query_messages() uses.
    """
    with _db() as c:
        rows = c.execute("SELECT * FROM held WHERE status = 'pending' ORDER BY id DESC LIMIT ?",
                         (max(1, min(limit, 500)),))
        return [dict(r) for r in rows]


def pending_in_chat(chat_id: str) -> list[dict]:
    """Every reply still waiting in one conversation, newest first.

    Uncapped, unlike pending(): the caller is resolving one conversation's
    duplicate drafts, and a limit there would leave the rows past it pending
    for ever — the very thing that resolution exists to prevent. A single
    conversation's unanswered drafts are bounded by how fast one person texts.
    """
    if not chat_id:
        return []
    with _db() as c:
        rows = c.execute("SELECT * FROM held WHERE status = 'pending' AND chat_id = ? ORDER BY id DESC",
                         (chat_id,))
        return [dict(r) for r in rows]


def pending_count() -> int:
    """How many replies are waiting, whatever pending() was asked for.

    The Held badge counted the rows pending() returned, so once fifty people
    were waiting it read "50" and stayed there however far behind the owner
    fell — the one number that should have told them it was getting worse.
    """
    with _db() as c:
        return c.execute("SELECT COUNT(*) FROM held WHERE status = 'pending'").fetchone()[0]


def get(item_id: int) -> dict | None:
    with _db() as c:
        row = c.execute("SELECT * FROM held WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None


def mark_edited(item_id: int) -> None:
    """Record that the owner rewrote this draft before sending it.

    Called at send time rather than inferred later: message_log keeps what
    went out and held keeps what she wrote, but matching the two needs a fuzzy
    join on chat and time that breaks on collapsed duplicates and repeat
    sends. One boolean, written where both texts are already in hand.
    """
    with _db() as c:
        c.execute("UPDATE held SET was_edited = 1 WHERE id = ?", (item_id,))


def transition(item_id: int, expected: str, new: str) -> bool:
    """Atomic status change. Moving pending -> sending first means a double
    click or a retry can never send the same reply twice."""
    with _db() as c:
        cur = c.execute("UPDATE held SET status = ? WHERE id = ? AND status = ?", (new, item_id, expected))
        return cur.rowcount == 1


# ---- audit ----

def log(kind: str, target: str = "") -> None:
    with _db() as c:
        c.execute("INSERT INTO audit (ts, kind, target) VALUES (?, ?, ?)", (time.time(), kind, target))


def recent_audit(limit: int = 50) -> list[dict]:
    with _db() as c:
        rows = c.execute("SELECT ts, kind, target FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


# ---- conversation memory (survives restarts; only the last `keep` messages per chat) ----

def add_message(chat_id: str, role: str, content: str, keep: int = 16) -> None:
    with _db() as c:
        c.execute("INSERT INTO messages (ts, chat_id, role, content) VALUES (?, ?, ?, ?)",
                  (time.time(), chat_id, role, content))
        c.execute("""DELETE FROM messages WHERE chat_id = ? AND id NOT IN
                     (SELECT id FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?)""",
                  (chat_id, chat_id, keep))


def get_messages(chat_id: str, limit: int = 16) -> list[dict]:
    with _db() as c:
        rows = c.execute("SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                         (chat_id, limit)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def clear_messages(chat_id: str) -> None:
    with _db() as c:
        c.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))


# ---- per-contact modes: "auto" (default) | "hold" (always ask me) | "ignore" ----

MODES = ("auto", "hold", "ignore")


def set_mode(name: str, mode: str) -> None:
    """Record a choice made about one person. Always stored, even when it
    matches today's default.

    It used to delete the row in that case, on the reasoning that a row is only
    needed for a choice that differs. But that cannot tell "I chose this" from
    "I never decided", so changing the default silently moved people who had
    been chosen deliberately — and with the default on "reply automatically",
    choosing someone stored nothing at all.
    """
    if mode not in MODES:
        raise ValueError(f"bad mode: {mode}")
    with _db() as c:
        c.execute("INSERT INTO contact_modes (name, mode) VALUES (?, ?) "
                  "ON CONFLICT(name) DO UPDATE SET mode = excluded.mode", (name.strip().lower(), mode))


def set_modes(names: list[str], mode: str) -> int:
    """Record one choice for many people at once. Returns how many rows were written.

    One transaction for the whole selection: choosing forty people and having
    half of them take the setting is worse than none of them taking it, because
    nothing on the page then says which half. Names are normalised and
    de-duplicated exactly as set_mode does it, so the two cannot disagree about
    who a row belongs to.
    """
    if mode not in MODES:
        raise ValueError(f"bad mode: {mode}")
    # dict.fromkeys de-duplicates and keeps the order the owner picked them in,
    # which is what any report of "wrote 3" should be able to list back.
    wanted = list(dict.fromkeys(n.strip().lower() for n in names if (n or "").strip()))
    if not wanted:
        return 0
    with _db() as c:
        c.executemany("INSERT INTO contact_modes (name, mode) VALUES (?, ?) "
                      "ON CONFLICT(name) DO UPDATE SET mode = excluded.mode",
                      [(name, mode) for name in wanted])
    return len(wanted)


def clear_mode(name: str) -> None:
    """Forget a choice, so this person follows the default again."""
    with _db() as c:
        c.execute("DELETE FROM contact_modes WHERE name = ?", (name.strip().lower(),))


def default_mode() -> str:
    """What someone you have not chosen gets.

    "ignore": their message is dropped before the agent runs, so nobody's
    conversation is transcribed, stored or sent to a model until you pick them.
    Choosing who the assistant handles should be something you do on purpose,
    one person at a time, not something that happens to a whole address book
    the moment it is synced.
    """
    mode = get_setting("default_contact_mode", "ignore")
    return mode if mode in MODES else "ignore"


def set_default_mode(mode: str) -> None:
    set_setting("default_contact_mode", mode)


def get_mode(name: str | None) -> str:
    if not name:
        return default_mode()
    with _db() as c:
        row = c.execute("SELECT mode FROM contact_modes WHERE name = ?", (name.strip().lower(),)).fetchone()
        return row["mode"] if row else default_mode()


# ---- per-group settings: how she behaves in one group chat ----
# Deliberately separate from the per-contact modes above. The words differ
# because the questions differ: a contact is "auto | hold | ignore", a group is
# "never | only when addressed | every message" crossed with "send it | show me
# first". app/groups.py owns the vocabulary and the defaults; this only stores
# them, and normalises on the way out so a value edited by hand into the
# database cannot be the thing that switches a group on.

def group_policy(chat_id: str) -> tuple[str, str]:
    """This group's (mode, delivery). A group nobody has decided about comes
    back as the safe pair, which is "leave alone"."""
    from app import groups
    if not chat_id:
        return groups.DEFAULT_MODE, groups.DEFAULT_DELIVERY
    with _db() as c:
        row = c.execute("SELECT mode, delivery FROM group_modes WHERE chat_id = ?",
                        (chat_id,)).fetchone()
    if not row:
        return groups.DEFAULT_MODE, groups.DEFAULT_DELIVERY
    return groups.normalise_mode(row["mode"]), groups.normalise_delivery(row["delivery"])


def set_group_policy(chat_id: str, mode: str, delivery: str) -> None:
    """Record a decision about one group. Both values are validated here rather
    than coerced, because a misspelt mode that silently became "open" would be
    the worst possible failure in this table."""
    from app import groups
    if not chat_id:
        raise ValueError("a group needs a chat id")
    if mode not in groups.MODES:
        raise ValueError(f"bad group mode: {mode}")
    if delivery not in groups.DELIVERY:
        raise ValueError(f"bad group delivery: {delivery}")
    with _db() as c:
        c.execute("INSERT INTO group_modes (chat_id, mode, delivery) VALUES (?, ?, ?) "
                  "ON CONFLICT(chat_id) DO UPDATE SET mode = excluded.mode, "
                  "delivery = excluded.delivery", (chat_id, mode, delivery))


def all_group_policies() -> dict[str, dict[str, str]]:
    """Every group that has been decided about, for the dashboard to render."""
    with _db() as c:
        return {r["chat_id"]: {"mode": r["mode"], "delivery": r["delivery"]}
                for r in c.execute("SELECT chat_id, mode, delivery FROM group_modes")}


def clear_group_policy(chat_id: str) -> None:
    """Forget a group, so it follows the default again — which is silence."""
    with _db() as c:
        c.execute("DELETE FROM group_modes WHERE chat_id = ?", (chat_id,))


def all_modes() -> dict[str, str]:
    with _db() as c:
        return {r["name"]: r["mode"] for r in c.execute("SELECT name, mode FROM contact_modes")}


# ---- per-chat direction: how she writes in one conversation ----
# The modes above decide WHETHER she answers someone. This decides HOW she
# writes to them: one short standing instruction in the owner's own words —
# "keep it short this week, I'm travelling", "don't agree to anything about
# money", "be warmer, she's upset" — which every reply in that conversation is
# then written under. Everything else about her voice (data/kb/about_me.md and
# data/style_examples.md) is global, so this was the one thing that could not
# be said about a single person.
#
# Why a table of its own, and why neither existing one can hold it (§D3.1):
# * `settings` is one flat key/value row per global switch. A per-chat
#   direction there means a key built out of the chat id — a table pretending
#   to be a dictionary — and "when was this set" would have to be packed into
#   `value` as JSON, which is the one thing §D3.1 asks not to do with a column
#   that is read on every message. "Which chats are steered" would become a
#   LIKE scan over every setting.
# * `contact_modes` keys on the contact NAME, so a group chat has nowhere to
#   sit — the same reason `group_modes` exists — and its one column is a
#   validated three-value vocabulary. Free text beside it would put two
#   unrelated decisions in one row.
# * `group_modes` is groups only, and holds the two questions a group asks.
# Expected rows: one per conversation deliberately steered, so tens. It is
# bounded by the people the owner talks to and never grows with messages.
# Written by the dashboard (POST /api/wa/direction); read by /webhook on every
# incoming message, and by the dashboard to show that a chat is being steered.
# chat_id is the primary key and the only way a row is ever looked up, so this
# needs no index of its own.

# How long a direction may be. It is part of the instructions on every single
# message in that conversation, so it is a sentence or two and not a second
# about_me.md: 500 characters is about four lines of dictated speech. Enforced
# here as well as at the route, because the route is not the only way into this
# function and a prompt is the wrong place to discover a long paste.
MAX_DIRECTION_CHARS = 500


def get_direction(chat_id: str) -> dict | None:
    """This conversation's standing instruction and when it was last changed,
    or None when nobody has steered it.

    The timestamp comes back with the text because a direction is invisible
    once set: "be brief, I'm travelling" is right for a week and wrong a month
    later, and how long it has been there is the only thing that says which.
    """
    if not chat_id:
        return None
    with _db() as c:
        row = c.execute("SELECT direction, set_at FROM chat_directions WHERE chat_id = ?",
                        (chat_id,)).fetchone()
    if not row or not row["direction"]:
        return None
    return {"text": row["direction"], "set_at": row["set_at"]}


def set_direction(chat_id: str, direction: str) -> bool:
    """Steer one conversation, or stop steering it. True when a direction was
    stored, False when it was cleared.

    Blank clears instead of storing: an empty instruction block in the prompt
    would be a line of ceremony saying nothing, and "" must not become a second
    way of meaning "no direction" that reads differently to the model. The
    length is checked rather than truncated, for the reason set_group_policy()
    validates rather than coerces — a direction silently cut in half mid
    sentence could say the opposite of what was meant.
    """
    if not chat_id:
        raise ValueError("a direction needs a chat id")
    text = direction.strip()
    if len(text) > MAX_DIRECTION_CHARS:
        raise ValueError(f"a direction is at most {MAX_DIRECTION_CHARS} characters")
    if not text:
        clear_direction(chat_id)
        return False
    with _db() as c:
        c.execute("INSERT INTO chat_directions (chat_id, direction, set_at) VALUES (?, ?, ?) "
                  "ON CONFLICT(chat_id) DO UPDATE SET direction = excluded.direction, "
                  "set_at = excluded.set_at", (chat_id, text, time.time()))
    return True


def clear_direction(chat_id: str) -> None:
    """Stop steering this conversation, so it reads like every other one."""
    with _db() as c:
        c.execute("DELETE FROM chat_directions WHERE chat_id = ?", (chat_id,))


def all_directions() -> dict[str, dict]:
    """Every conversation being steered, read once for a whole list of chats."""
    with _db() as c:
        return {r["chat_id"]: {"text": r["direction"], "set_at": r["set_at"]}
                for r in c.execute("SELECT chat_id, direction, set_at FROM chat_directions")}


# ---- settings (global kill switch) ----

def get_setting(key: str, default: str = "") -> str:
    with _db() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _db() as c:
        c.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def auto_reply_enabled() -> bool:
    return get_setting("auto_reply", "1") == "1"


def self_log_enabled() -> bool:
    """When on, every reply the assistant sends by itself is copied to the
    owner's own WhatsApp chat, so their phone shows what went out as them."""
    return get_setting("self_log", "0") == "1"


def direct_send_enabled() -> bool:
    """When on, "send X to Mom" in the dashboard goes out without a confirmation card."""
    return get_setting("direct_send", "1") == "1"


# ---- message log: what people sent you and what went out (for "show me today's messages") ----
# Only chats the assistant handled are here. Stored on disk, so it is purged after
# MESSAGE_RETENTION_DAYS (default 30).

def retention_days() -> int:
    try:
        return max(1, int(os.getenv("MESSAGE_RETENTION_DAYS", "30")))
    except ValueError:
        return 30


def log_message(chat_id: str, name: str | None, direction: str, body: str, outcome: str = "") -> None:
    """direction: "in" (they wrote to you) or "out" (a message that was sent as you)."""
    with _db() as c:
        c.execute("INSERT INTO message_log (ts, chat_id, name, direction, body, outcome) VALUES (?, ?, ?, ?, ?, ?)",
                  (time.time(), chat_id or "", name or "", direction, body, outcome))
    if time.time() - _last_purge > 86400:
        purge_messages()


def purge_messages(days: int | None = None) -> int:
    global _last_purge
    cutoff = time.time() - (days or retention_days()) * 86400
    with _db() as c:
        deleted = c.execute("DELETE FROM message_log WHERE ts < ?", (cutoff,)).rowcount
    _last_purge = time.time()
    return deleted


def outgoing_for_chat(chat_id: str, since: float, limit: int = 300) -> list[dict]:
    """What this conversation was sent by the assistant or from the dashboard.

    Used to tell a reply the assistant wrote by itself apart from one the
    owner typed on their phone, which WhatsApp's own thread cannot do.
    """
    with _db() as c:
        rows = c.execute(
            "SELECT ts, body, outcome, direction FROM message_log "
            "WHERE chat_id = ? AND direction = 'out' AND ts >= ? "
            "ORDER BY id DESC LIMIT ?",
            (chat_id, since, max(1, min(limit, 500))))
        return [dict(r) for r in rows]


def query_messages(name: str | None = None, since: float | None = None, until: float | None = None,
                   direction: str | None = None, limit: int = 100) -> list[dict]:
    sql, params = "SELECT ts, chat_id, name, direction, body, outcome FROM message_log WHERE 1=1", []
    if name:
        sql += " AND LOWER(name) = LOWER(?)"
        params.append(name)
    if since is not None:
        sql += " AND ts >= ?"
        params.append(since)
    if until is not None:
        sql += " AND ts < ?"
        params.append(until)
    if direction in ("in", "out"):
        sql += " AND direction = ?"
        params.append(direction)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    with _db() as c:
        return [dict(r) for r in c.execute(sql, params)]


# ---- searching the message log by what was said ----
# "What did the landlord say about the water bill" was unanswerable here: the
# text was on disk and nothing could look inside it. The obvious LIKE '%term%'
# cannot use an index, so every search would read every body in the log.
#
# So the one extra table in this schema (§D3.1): an external-content FTS5
# index over message_log.body. External content means the bodies stay in
# message_log and this holds only the inverted index over them — no second
# copy of anyone's messages. Its row count tracks message_log exactly, it is
# written only by the three triggers below, and it is read only by
# search_messages(). Growth is message_log's growth, which the retention sweep
# bounds at MESSAGE_RETENTION_DAYS.
#
# Triggers rather than an extra write inside log_message(), because
# message_log is also deleted from (purge_messages) and updated directly —
# an index kept in step by hand would drift into offering the owner messages
# that have already been purged.

# unicode61 is named rather than left to the default so the matching rules are
# pinned here instead of inherited from whatever SQLite is in the image.
# "remove_diacritics 2" is what makes café, cafe and CAFÉ one word. It folds Latin
# accents only; combining marks in other scripts stay part of the word, and
# search_messages() spells out what that means for searching.
_FTS_TOKENIZER = "unicode61 remove_diacritics 2"

MIN_SEARCH_CHARS = 2
# How much of a long message comes back beside a match. Twelve tokens is about
# a line of chat: enough to read the sentence the word is in, little enough
# that a page of results stays a page and not a transcript.
SNIPPET_TOKENS = 12
SNIPPET_ELLIPSIS = "…"


def _add_message_search(c) -> int:
    """Create the search index and its triggers, and fill it in once. Returns
    how many existing messages it indexed, which is 0 on every boot but the
    one where the index first appears.

    Written in the defensive style of _add_column(): an assistant.db that
    predates this runs through it and comes out searchable, with the month of
    messages it already holds indexed rather than only what arrives next.
    """
    # Whether the index is new is the backfill's guard. A settings flag would
    # say "we did this once" even after someone dropped the table to rebuild
    # it; sqlite_master says what is actually there. Either way it cannot run
    # on every boot, which on a full log would mean re-indexing a month of
    # messages every restart.
    fresh = not c.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'message_log_fts'").fetchone()
    c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS message_log_fts USING fts5("
              f"body, content='message_log', content_rowid='id', tokenize='{_FTS_TOKENIZER}')")
    c.execute("""CREATE TRIGGER IF NOT EXISTS message_log_fts_insert
                 AFTER INSERT ON message_log BEGIN
                   INSERT INTO message_log_fts (rowid, body) VALUES (new.id, new.body);
                 END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS message_log_fts_delete
                 AFTER DELETE ON message_log BEGIN
                   INSERT INTO message_log_fts (message_log_fts, rowid, body)
                   VALUES ('delete', old.id, old.body);
                 END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS message_log_fts_update
                 AFTER UPDATE ON message_log BEGIN
                   INSERT INTO message_log_fts (message_log_fts, rowid, body)
                   VALUES ('delete', old.id, old.body);
                   INSERT INTO message_log_fts (rowid, body) VALUES (new.id, new.body);
                 END""")
    if not fresh:
        return 0
    return c.execute(
        "INSERT INTO message_log_fts (rowid, body) SELECT id, body FROM message_log").rowcount


# The pieces a character can be built from that mean nothing on their own: the
# combining marks, the five emoji skin-tone modifiers, the zero-width joiner
# that holds a family emoji together, the variation selectors and the enclosing
# keycap.
#
# FTS5's snippet() cuts on token boundaries, and to unicode61 a run of emoji is
# one long gap between two tokens — so a cut lands inside one. Measured, not
# assumed: a window around "two" in "one 👩🏽‍💻 two" comes back as "…🏽‍💻 two",
# a skin tone and a laptop with nobody in front of them. The bridge learned
# this in JavaScript (wa_read.js cutTo) and tts.py learned it in Python. Third
# time, same rule: a grapheme cluster is kept whole or left out.
_SKIN_TONES = frozenset(chr(cp) for cp in range(0x1F3FB, 0x1F400))
_REGIONAL_INDICATORS = frozenset(chr(cp) for cp in range(0x1F1E6, 0x1F200))
# Joiners that need something after them as well as before, so one left at the
# end of a window is as broken as one left at the start.
_JOINERS = frozenset("\u200d\ufe0e\ufe0f\u20e3")   # ZWJ, the two variation selectors, keycap


def _continues_previous(ch: str) -> bool:
    """Whether this character is only ever part of the one before it."""
    return (unicodedata.category(ch) in ("Mn", "Mc", "Me")
            or ch in _SKIN_TONES or ch in _JOINERS)


def _regional_run(text: str) -> int:
    """How many regional indicators `text` starts with."""
    run = 0
    while run < len(text) and text[run] in _REGIONAL_INDICATORS:
        run += 1
    return run


def _whole_characters(text: str) -> str:
    """`text` with half a character at either end taken off."""
    start, end = 0, len(text)
    while start < end and _continues_previous(text[start]):
        start += 1
    while end > start and text[end - 1] in _JOINERS:
        end -= 1
    text = text[start:end]
    # A flag is exactly two regional indicators and nothing else is built out
    # of them, so an odd run at either edge is half a flag.
    if _regional_run(text) % 2:
        text = text[1:]
    if _regional_run(text[::-1]) % 2:
        text = text[:-1]
    return text


def _trim_snippet(raw: str) -> str:
    """One snippet() window, keeping its ellipses and dropping half characters."""
    head = SNIPPET_ELLIPSIS if raw.startswith(SNIPPET_ELLIPSIS) else ""
    middle = raw[len(head):]
    tail = SNIPPET_ELLIPSIS if middle.endswith(SNIPPET_ELLIPSIS) else ""
    return head + _whole_characters(middle[:len(middle) - len(tail)]) + tail


def _fts_phrase(term: str) -> str:
    """What the owner typed, as an FTS5 MATCH expression.

    The whole term becomes one quoted phrase, so none of MATCH's own
    vocabulary can leak out of it: AND, OR, NOT, NEAR, a bare "-", a colon,
    brackets and a stray "*" are all just text inside quotes, and the only
    character quotes themselves care about is the double quote, which FTS5
    escapes by doubling. That is the whole of the escaping — the term still
    travels as a bound parameter, never spliced into SQL (§B11).

    The trailing "*" makes the last token of the phrase a prefix, so "wat"
    finds "water" while he is still typing.
    """
    return '"' + term.replace('"', '""') + '" *'


def search_messages(term: str, name: str | None = None, since: float | None = None,
                    until: float | None = None, direction: str | None = None,
                    limit: int = 50, before_id: int | None = None) -> list[dict]:
    """Messages whose text matches `term`, newest first, with the same filters
    and the same ceiling query_messages() has.

    What matches, measured against this corpus rather than assumed:

    * Whole words, anywhere in the message, and the last word by prefix. "bill"
      finds "the water bill"; "wat" finds "water"; "ater" finds nothing,
      because this is a word index and not a substring scan.
    * Several words are a phrase: "water bill" wants them side by side.
    * Case and Latin accents are folded both ways — landlord/LANDLORD,
      café/cafe/CAFÉ — which is more than LIKE would give, since SQLite's LIKE
      only folds ASCII.
    * Kiswahili and Sheng behave as English does: both are Latin script, so
      "maji", "MAJI" and "pes" all find "Nitatuma pesa ya maji kesho".
    * A word index is a word index, which Kiswahili feels more than English
      does: number is marked on the front of the word, so "mtoto" does not
      find "watoto" and the stem "toto" they share finds neither. The
      apostrophe in "ng'ombe" is a separator too, so that word is indexed as
      "ng" and "ombe" and the spelling "ngombe" does not find it. A prefix
      does work ("wato", "ng'omb"), which is the way in.
    * remove_diacritics 2 folds Latin accents only. Combining characters
      from anywhere else stay part of the token, which is why a script that
      writes its vowels or tones as marks has two different words for the
      marked and unmarked spellings. Nothing in this corpus is written that
      way; the nearest it comes is the skin-tone modifier inside an emoji,
      which is a token in its own right while the picture around it is not.
    * No matching on emoji or punctuation: unicode61 treats both as the spaces
      between words, so a search for "😂" or "%" finds nothing at all rather
      than everything. For the same reason "%" and "_" cannot act as wildcards
      the way they would in a LIKE pattern — they are not pattern characters
      here, they are separators — and "100%" finds the word "100".
    * CJK runs as one token, so "明天" does not find "我们明天见面". Noted
      because it is a real gap, not because this corpus needs it.

    `before_id` is the cursor: pass the id of the last row you were given and
    the next page carries on below it. Keyed on id rather than an offset
    because messages arrive while the owner reads, and an offset would skip or
    repeat a row each time one did.
    """
    wanted = " ".join((term or "").split())
    if len(wanted) < MIN_SEARCH_CHARS:
        # Refused here rather than answered with the whole table: one character
        # and a prefix match is every message in the log.
        raise ValueError(f"Type at least {MIN_SEARCH_CHARS} characters to search.")

    sql = ("SELECT m.id, m.ts, m.chat_id, m.name, m.direction, m.body, m.outcome, "
           "snippet(message_log_fts, 0, '', '', ?, ?) AS snippet "
           "FROM message_log_fts JOIN message_log AS m ON m.id = message_log_fts.rowid "
           "WHERE message_log_fts MATCH ?")
    # The FTS table cannot be aliased — "f MATCH ?" is "no such column: f" —
    # so it is spelled out and only message_log carries an alias.
    params: list = [SNIPPET_ELLIPSIS, SNIPPET_TOKENS, _fts_phrase(wanted)]
    if name:
        sql += " AND LOWER(m.name) = LOWER(?)"
        params.append(name)
    if since is not None:
        sql += " AND m.ts >= ?"
        params.append(since)
    if until is not None:
        sql += " AND m.ts < ?"
        params.append(until)
    if direction in ("in", "out"):
        sql += " AND m.direction = ?"
        params.append(direction)
    if before_id is not None:
        sql += " AND m.id < ?"
        params.append(before_id)
    sql += " ORDER BY m.id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))

    with _db() as c:
        rows = [dict(r) for r in c.execute(sql, params)]
    for row in rows:
        row["snippet"] = _trim_snippet(row["snippet"])
    return rows
