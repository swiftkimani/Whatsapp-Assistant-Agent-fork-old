"""Telling a reply the assistant sent apart from one the owner typed."""
from app import sent_marks


def log_row(ts, body, outcome, direction="out"):
    return {"ts": ts, "body": body, "outcome": outcome, "direction": direction}


def thread_msg(ts, body, from_me=True):
    return {"ts": ts, "body": body, "from_me": from_me}


def test_an_automatic_reply_is_marked():
    messages = [thread_msg(1000, "Niko njiani, nitafika saa nne.")]
    sent_marks.mark(messages, [log_row(1000, "Niko njiani, nitafika saa nne.", "auto")])
    assert messages[0]["sent_by"] == "auto"


def test_an_approved_draft_is_marked_differently():
    messages = [thread_msg(1000, "Nitatuma leo jioni.")]
    sent_marks.mark(messages, [log_row(1000, "Nitatuma leo jioni.", "held_sent")])
    assert messages[0]["sent_by"] == "held_sent"


def test_a_message_the_owner_typed_on_their_phone_is_not_marked():
    messages = [thread_msg(1000, "Something typed on the phone")]
    sent_marks.mark(messages, [log_row(1000, "A different reply entirely", "auto")])
    assert messages[0]["sent_by"] == ""


def test_an_incoming_message_is_never_marked():
    """Guards against a reply echoed back by the other person being claimed."""
    messages = [thread_msg(1000, "Sawa", from_me=False)]
    sent_marks.mark(messages, [log_row(1000, "Sawa", "auto")])
    assert messages[0]["sent_by"] == ""


def test_the_send_queue_delay_does_not_break_the_match():
    """The log row is written when the text is produced; the queue then spaces
    the send out by up to about half a minute."""
    messages = [thread_msg(1035, "Sawa, tutaonana kesho.")]
    sent_marks.mark(messages, [log_row(1000, "Sawa, tutaonana kesho.", "auto")])
    assert messages[0]["sent_by"] == "auto"


def test_the_same_words_a_day_later_are_not_claimed():
    messages = [thread_msg(1000 + 86400, "Sawa")]
    sent_marks.mark(messages, [log_row(1000, "Sawa", "auto")])
    assert messages[0]["sent_by"] == ""


def test_two_identical_replies_consume_two_separate_rows():
    messages = [thread_msg(1000, "Sawa"), thread_msg(1200, "Sawa"), thread_msg(1400, "Sawa")]
    sent_marks.mark(messages, [log_row(1000, "Sawa", "auto"), log_row(1200, "Sawa", "held_sent")])
    assert [m["sent_by"] for m in messages] == ["auto", "held_sent", ""]


def test_a_clipped_message_still_matches_what_was_sent():
    """The thread clips a long message and adds an ellipsis; the log keeps it
    whole, so the two only ever agree on a prefix."""
    whole = "Habari yako. " + ("x" * 5000)
    messages = [thread_msg(1000, whole[:200] + "…")]
    sent_marks.mark(messages, [log_row(1000, whole, "auto")])
    assert messages[0]["sent_by"] == "auto"


def test_photos_and_voice_notes_are_not_all_matched_to_one_another():
    """Every message with no text of its own shares the same empty body, so an
    empty fingerprint identifies nothing and must match nothing."""
    messages = [thread_msg(1000, ""), thread_msg(1010, "")]
    sent_marks.mark(messages, [log_row(1000, "", "auto")])
    assert [m["sent_by"] for m in messages] == ["", ""]


def test_whitespace_differences_do_not_prevent_a_match():
    messages = [thread_msg(1000, "Sawa  bwana\n\nnitakuja")]
    sent_marks.mark(messages, [log_row(1000, "Sawa bwana nitakuja", "auto")])
    assert messages[0]["sent_by"] == "auto"


def test_an_outcome_we_do_not_speak_for_is_left_alone():
    messages = [thread_msg(1000, "Sawa")]
    sent_marks.mark(messages, [log_row(1000, "Sawa", "something_else")])
    assert messages[0]["sent_by"] == ""


def test_an_inbound_log_row_cannot_mark_an_outgoing_message():
    messages = [thread_msg(1000, "Sawa")]
    sent_marks.mark(messages, [log_row(1000, "Sawa", "auto", direction="in")])
    assert messages[0]["sent_by"] == ""


def test_every_message_gets_the_field_even_with_nothing_logged():
    """The dashboard reads it on every bubble, so it must always be present."""
    messages = [thread_msg(1000, "a"), thread_msg(1001, "b", from_me=False)]
    sent_marks.mark(messages, [])
    assert all("sent_by" in m for m in messages)
