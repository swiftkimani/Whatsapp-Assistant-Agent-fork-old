"""The conversation turns a reply is asked for, and the blank-message guard.

The API rejects a conversation that starts with our own reply, and rejects an
empty turn. Neither is hypothetical here: the rolling memory window is just the
last N rows, and a held message stores the incoming text with no reply after it.
"""
from app.llm import _normalize_history, chat_completion


def test_history_must_start_with_the_contacts_turn():
    out = _normalize_history([
        {"role": "assistant", "content": "sawa"},      # window began on our reply
        {"role": "user", "content": "uko wapi"},
        {"role": "assistant", "content": "niko campus"},
    ])
    assert [m["role"] for m in out] == ["user", "assistant"]


def test_history_of_only_our_own_replies_becomes_empty():
    assert _normalize_history([{"role": "assistant", "content": "hi"}]) == []


def test_consecutive_turns_from_one_side_are_kept():
    # Two messages in a row with no reply between them: what a hold leaves behind.
    out = _normalize_history([{"role": "user", "content": "a"}, {"role": "user", "content": "b"}])
    assert [m["content"] for m in out] == ["a", "b"]


def test_blank_content_and_unknown_roles_are_dropped():
    out = _normalize_history([
        {"role": "user", "content": "   "},
        {"role": "system", "content": "not a conversation turn"},
        {"role": "user", "content": "real"},
        {"role": "assistant", "content": None},
    ])
    assert out == [{"role": "user", "content": "real"}]


def test_no_history_is_fine():
    assert _normalize_history(None) == []
    assert _normalize_history([]) == []


def test_a_blank_message_is_held_without_calling_the_model():
    """A voice note Whisper heard nothing in. There is nothing to reply to, and
    an empty turn would be rejected, so it must fail closed — and must not need
    an API key to do so."""
    out = chat_completion("   ")
    assert (out["reply"], out["grounded"], out["needs_owner"]) == ("", False, True)
    assert out["why"]        # the Held tab says what happened rather than showing a blank card
