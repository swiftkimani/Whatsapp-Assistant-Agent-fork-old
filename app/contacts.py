"""contacts.json maps a name to a phone number or a full chat id.

Lives in the project root by default. CONTACTS_FILE moves it, which is how the
backend and the bridge share one copy when they run as separate containers.
"""
import json
import os
import re
from pathlib import Path

CONTACTS_FILE = Path(os.getenv("CONTACTS_FILE")
                     or Path(__file__).resolve().parent.parent / "contacts.json")


def load() -> dict[str, str]:
    try:
        data = json.loads(CONTACTS_FILE.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def names() -> list[str]:
    return list(load().keys())


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _same_number(a: str, b: str) -> bool:
    """Is this one human reached two ways?

    One number must be the other written nationally: drop the trunk zero, and
    the shorter has to be a tail of the longer.

    A plain last-9 comparison is not enough. Brazilian 5511987654321 and
    Spanish 34987654321 end in the same nine digits and are two different
    people, and treating them as one hands a stranger whatever mode belongs to
    the contact they collided with — their messages are filed under that name,
    and choosing "let the assistant answer" on one silently reconfigures the
    other.

    Suffix comparison alone is not enough either: a national number keeps the
    trunk zero in some countries ("201012345678" ends with "01012345678") but
    replaces the country code with it in others, so "254712345678" and
    "0712345678" — one Kenyan line — never matched, and that person quietly
    got the default mode instead of the one chosen for them.
    """
    if not (a and b):
        return False
    if a == b:
        return True
    longer, shorter = (a, b) if len(a) >= len(b) else (b, a)
    national = shorter[1:] if shorter.startswith("0") else shorter
    # Enough digits left to be a subscriber number rather than a coincidence.
    return len(national) >= 7 and len(longer) > len(national) and longer.endswith(national)


def name_for(chat_id: str, number: str | None = None, book: dict[str, str] | None = None) -> str | None:
    """Reverse lookup: which contacts.json name does this chat belong to?

    `book` lets a caller resolving many chats at once read the file a single
    time instead of once per chat.
    """
    base = chat_id.split("@")[0]
    for name, value in (load() if book is None else book).items():
        value = value.strip()
        if "@" in value:
            if value == chat_id or value.split("@")[0] == base:
                return name
        else:
            d = _digits(value)
            if _same_number(d, _digits(base)) or _same_number(d, _digits(number or "")):
                return name
    return None


def canonical(name: str) -> str | None:
    """Match a name case-insensitively and return it exactly as written in contacts.json."""
    return {n.strip().lower(): n for n in names()}.get((name or "").strip().lower())


def _normalise(value: str) -> str:
    """A phone number as digits, or a full chat id kept as written.

    "+254 712 345 678", "0712345678" and "254712345678" are all one line, and
    an "@lid" privacy id is a valid recipient that has no number at all.
    """
    value = (value or "").strip()
    if "@" in value:
        return value
    return _digits(value)


def add(name: str, number: str) -> str:
    """Put one person in contacts.json by hand, and return the name stored.

    The same rule the phone sync follows: only ever adds. An existing name is
    never renamed or repointed, because this file is the list the dashboard may
    message and a careless write could redirect a message to someone else.

    Raises ValueError with a sentence fit to show the owner.
    """
    name = " ".join((name or "").split())
    value = _normalise(number)
    if not name:
        raise ValueError("Give the person a name.")
    if len(name) > 60:
        raise ValueError("That name is too long.")
    if "@" not in value and len(value) < 7:
        raise ValueError("That does not look like a phone number.")
    # The bridge resolves a stored number with client.getNumberId(), which wants
    # the international form. A national number ("0712…") resolves to nobody, so
    # the entry would look fine on the list and be unreachable when it mattered.
    # Refusing is the loud failure; storing it is the quiet one.
    if "@" not in value and value.startswith("0"):
        raise ValueError("Start with the country code, like 254712345678, not 0712345678.")

    # Re-read immediately before writing: the bridge writes this file too, when
    # you sync your address book, and whoever writes last would otherwise drop
    # the other's entry.
    book = load()
    if canonical(name):
        raise ValueError(f"{canonical(name)} is already on the list.")
    for existing, stored in book.items():
        if _normalise(stored) == value or _same_number(_digits(stored), _digits(value)):
            raise ValueError(f"That number is already saved as {existing}.")


    book[name] = value
    tmp = CONTACTS_FILE.with_suffix(f".{os.getpid()}.tmp")
    CONTACTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(book, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Rename over the old file, so an interrupted write can never leave a
    # truncated recipient list behind.
    os.replace(tmp, CONTACTS_FILE)
    return name


def add_for_chat(chat_id: str, suggested: str) -> str:
    """Put the person behind a WhatsApp chat on the list, and return the name.

    For choosing, on the WhatsApp tab, who the assistant answers: those chats
    include people who were never saved on the phone, and a per-person setting
    needs a name in this file to hang on.

    Returns the name already stored when there is one, so choosing the same
    person twice does not create a second entry.
    """
    existing = name_for(chat_id)
    if existing:
        return existing

    base = chat_id.split("@")[0].split(":")[0]
    # An "@lid" chat has no number to store, and the id itself is a valid
    # recipient, so it is kept whole.
    number = chat_id if chat_id.endswith("@lid") else _digits(base)
    # WhatsApp has no name for some chats — a privacy id, or someone who has
    # never been saved — so the digits stand in rather than the raw id.
    name = " ".join((suggested or "").split())[:60] or _digits(base)[-9:] or "Unknown"

    try:
        return add(name, number)
    except ValueError:
        # The name is taken by somebody else. WhatsApp names are not unique and
        # two people really can both be "Mum", so the line is what tells them
        # apart — the same disambiguation the phone sync uses.
        return add(f"{name} ({_digits(base)[-4:]})", number)
