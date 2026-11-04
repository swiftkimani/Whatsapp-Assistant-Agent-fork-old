"""SQLite store (data/assistant.db): held replies, audit log, conversation
memory, per-contact modes and settings."""
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

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
        c.execute("""CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, target TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, chat_id TEXT, role TEXT, content TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id, id)")
        c.execute("CREATE TABLE IF NOT EXISTS contact_modes (name TEXT PRIMARY KEY, mode TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute("""CREATE TABLE IF NOT EXISTS message_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, chat_id TEXT, name TEXT,
            direction TEXT, body TEXT, outcome TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_message_log_ts ON message_log (ts)")
    purge_messages()


# ---- held replies ----

def add_held(chat_id: str, incoming: str, reply: str, reason: str = "",
             number: str | None = None, why: str = "") -> int:
    with _db() as c:
        cur = c.execute(
            "INSERT INTO held (ts, chat_id, incoming, reply, reason, number, why) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), chat_id, incoming, reply, reason, number or "", why))
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


def all_modes() -> dict[str, str]:
    with _db() as c:
        return {r["name"]: r["mode"] for r in c.execute("SELECT name, mode FROM contact_modes")}


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
