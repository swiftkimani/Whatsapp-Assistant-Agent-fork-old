"""The fail-closed rule: anything malformed must end up held, never sent."""
import json

import pytest

from app.llm import _parse_answer


def test_valid_answer():
    out = _parse_answer(json.dumps({"reply": " Niko njiani ", "grounded": True,
                                    "needs_owner": False, "why": "just a greeting"}))
    assert out == {"reply": "Niko njiani", "grounded": True, "needs_owner": False,
                   "why": "just a greeting", "quote": False}


def test_the_reason_is_optional_and_bounded():
    """An older or truncated answer without it must still parse, and a model
    that ignores the 12-word instruction must not fill the Held tab. The bound
    came down from 200 characters: the owner reads this at a glance beside the
    message, and a long one crowded out the message itself."""
    out = _parse_answer(json.dumps({"reply": "sawa", "grounded": True, "needs_owner": False}))
    assert out["why"] == "" and out["reply"] == "Sawa"
    long = _parse_answer(json.dumps({"reply": "sawa", "grounded": True,
                                     "needs_owner": False, "why": "x" * 500}))
    assert len(long["why"]) == 120


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null", '{"grounded": true}', '{"reply": null}'])
def test_malformed_output_fails_closed(raw):
    out = _parse_answer(raw)
    assert out["reply"] == "" and out["grounded"] is False and out["needs_owner"] is True


def test_flags_must_be_real_booleans():
    out = _parse_answer(json.dumps({"reply": "hi", "grounded": "true", "needs_owner": "false"}))
    assert out["grounded"] is False      # the string "true" is not trusted
    assert out["needs_owner"] is True    # only a literal false clears it


def test_missing_needs_owner_defaults_to_holding():
    assert _parse_answer('{"reply": "hi", "grounded": true}')["needs_owner"] is True


def test_the_model_can_ask_for_the_incoming_message_to_be_quoted():
    out = _parse_answer(json.dumps({"reply": "saa nne", "grounded": True,
                                    "needs_owner": False, "why": "answers one of three",
                                    "quote": True}))
    assert out["quote"] is True


def test_anything_but_a_real_true_means_no_quote():
    """A quote on the wrong message is worse than none, so this fails closed
    the same way the send/hold flags do."""
    for value in ["true", 1, None, "yes", {}]:
        out = _parse_answer(json.dumps({"reply": "sawa", "grounded": True,
                                        "needs_owner": False, "quote": value}))
        assert out["quote"] is False, value
