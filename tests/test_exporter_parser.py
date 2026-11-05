from app.export_parser import build_pairs, parse_messages

BRACKET = """[5:33 pm, 29/09/2026] Mona: uko wapi
[5:33 pm, 29/09/2026] Sam: niko campus
[5:34 pm, 29/09/2026] Mona: utamaliza saa ngapi
[5:34 pm, 29/09/2026] Sam: nitamaliza kama saa 6
[5:35 pm, 29/09/2026] Mona: sawa
[5:35 pm, 29/09/2026] Sam: fiti
"""

ANDROID = """29/09/2026, 5:33 pm - Messages and calls are end-to-end encrypted.
29/09/2026, 5:33 pm - Mona: niaje, uko aje
29/09/2026, 5:34 pm - Sam: niko fiti
na wewe?
29/09/2026, 5:35 pm - Mona: <Media omitted>
29/09/2026, 5:36 pm - Mona: sawa
29/09/2026, 5:36 pm - Sam: poa
"""


def test_bracket_format_pairs_and_digit_filter():
    pairs = build_pairs(parse_messages(BRACKET), owner="Sam")
    assert {"incoming": "uko wapi", "reply": "niko campus"} in pairs
    assert {"incoming": "sawa", "reply": "fiti"} in pairs
    assert all("6" not in p["reply"] for p in pairs)  # replies with digits are dropped
    assert len(pairs) == 2


def test_android_format_multiline_and_media_skipped():
    messages = parse_messages(ANDROID)
    assert ("Sam", "niko fiti na wewe?") in messages  # the second line was joined
    pairs = build_pairs(messages, owner="Sam")
    assert {"incoming": "niaje, uko aje", "reply": "niko fiti na wewe?"} in pairs
    assert {"incoming": "sawa", "reply": "poa"} in pairs  # media message ignored
    assert all("omitted" not in p["incoming"].lower() for p in pairs)


def test_unknown_owner_name_gives_no_pairs():
    assert build_pairs(parse_messages(BRACKET), owner="Nobody") == []


def test_mid_chat_system_lines_are_not_glued_onto_messages():
    text = (
        "29/09/2026, 5:33 pm - Mona: niaje, uko aje\n"
        "29/09/2026, 5:34 pm - Sam: niko fiti\n"
        "29/09/2026, 5:35 pm - Missed voice call\n"
        "29/09/2026, 5:36 pm - Mona: sawa\n"
        "29/09/2026, 5:36 pm - Sam: poa\n"
    )
    messages = parse_messages(text)
    assert ("Sam", "niko fiti") in messages  # nothing appended from the "Missed voice call" line
    assert len(messages) == 4
