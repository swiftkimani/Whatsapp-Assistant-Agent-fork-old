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

    Compare the last 9 digits, which is what the allowlist already does in both
    its copies. Comparing by suffix instead looked equivalent and was not: a
    national number keeps the trunk zero in some countries ("201012345678" ends
    with "01012345678") but replaces the country code with it in others, so
    "254712345678" and "0712345678" — one Kenyan line — did not match. The
    effect was quiet: name_for() returned nothing, and the person fell back to
    the default mode instead of the one set for them.
    """
    return bool(a and b) and min(len(a), len(b)) >= 9 and a[-9:] == b[-9:]


def name_for(chat_id: str, number: str | None = None) -> str | None:
    """Reverse lookup: which contacts.json name does this chat belong to?"""
    base = chat_id.split("@")[0]
    for name, value in load().items():
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
