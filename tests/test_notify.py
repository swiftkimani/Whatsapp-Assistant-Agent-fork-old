"""Telling the owner a draft is waiting, without becoming a nuisance.

Nothing here touches the network: the push service and the WhatsApp bridge are
both stood in for, and every clock is fixed. The one test that needs a real
P-256 key skips itself where `cryptography` is not installed — which is the
same condition under which the module reports web push as unavailable.
"""
import json
import os
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from app import notify, quiet_hours

AFTERNOON = datetime(2026, 11, 5, 14, 0, tzinfo=quiet_hours.LOCAL_TZ).timestamp()
MIDDLE_OF_THE_NIGHT = datetime(2026, 11, 5, 3, 0, tzinfo=quiet_hours.LOCAL_TZ).timestamp()

CHAT = "254700000001@c.us"
ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123-this-one-browser"
OTHER_ENDPOINT = "https://updates.push.services.mozilla.com/wpush/v2/zzz999"
SUBSCRIPTION = {"endpoint": ENDPOINT, "keys": {"p256dh": "BPublicKeyBytes", "auth": "AuthSecret"}}


@pytest.fixture(autouse=True)
def plain_environment(monkeypatch):
    """No keys, no quiet hours, default intervals — whatever the developer's
    .env happens to say. app.main loads it, and these tests share the process."""
    for name in ("QUIET_HOURS", "VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT",
                 "NOTIFY_SELF_MIN_MINUTES", "NOTIFY_PUSH_MIN_SECONDS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def api(fresh_db):
    """The notification routes, with the dashboard's login beside them so the
    real require_auth dependency is what the tests go through."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import dashboard

    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(notify.router)
    dashboard._failed_logins.clear()
    return TestClient(app)


@pytest.fixture()
def signed_in_api(api):
    assert api.post("/api/login", json={"password": "test-password"}).status_code == 200
    return api


def planned_nudges(monkeypatch) -> list[notify.Nudge]:
    """Collect what would be sent, instead of starting the sending thread."""
    nudges: list[notify.Nudge] = []
    monkeypatch.setattr(notify, "_dispatch", lambda nudge, now: nudges.append(nudge))
    return nudges


def recording_bridge(monkeypatch, status: int = 200) -> list[dict]:
    payloads: list[dict] = []

    def post_sync(path, payload, timeout):
        assert path == "/send-self"
        payloads.append(payload)
        return httpx.Response(status)

    monkeypatch.setattr(notify.bridge, "post_sync", post_sync)
    return payloads


def recording_push(monkeypatch, status: int = 201) -> list[str]:
    endpoints: list[str] = []

    def post_push(endpoint, headers):
        assert headers["Authorization"].startswith("vapid ")
        endpoints.append(endpoint)
        return status

    monkeypatch.setattr(notify, "_vapid_header", lambda endpoint, now: "vapid t=token, k=key")
    monkeypatch.setattr(notify, "_post_push", post_push)
    return endpoints


# ------------------------------------------------- nothing configured ----

def test_with_no_vapid_keys_push_is_off_and_says_why(signed_in_api):
    push = signed_in_api.get("/api/notify").json()["push"]
    assert push["configured"] is False
    assert push["enabled"] is False
    assert push["public_key"] == ""          # never offer a key that cannot sign
    assert "VAPID_PUBLIC_KEY" in push["reason"]


def test_push_status_reports_rather_than_raises_when_nothing_is_set_up(fresh_db):
    status = notify.push_status()
    assert status["configured"] is False and status["devices"] == 0
    assert status["reason"]


def test_push_says_so_when_the_signing_library_is_not_installed(monkeypatch):
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "A" * 86)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "B" * 43)
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:owner@example.com")
    monkeypatch.setattr(notify, "_import_crypto", lambda: None)
    assert "cryptography" in notify.push_problem()


def test_a_public_key_from_another_pair_is_refused_before_anything_is_sent(monkeypatch):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    mine = ec.generate_private_key(ec.SECP256R1())
    theirs = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setenv("VAPID_PRIVATE_KEY",
                       notify._b64(mine.private_numbers().private_value.to_bytes(32, "big")))
    monkeypatch.setenv("VAPID_PUBLIC_KEY", notify._b64(
        theirs.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)))
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:owner@example.com")
    assert "public half" in notify.push_problem()


def test_a_matching_key_pair_signs_a_token_the_push_service_can_verify(monkeypatch):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setenv("VAPID_PRIVATE_KEY",
                       notify._b64(key.private_numbers().private_value.to_bytes(32, "big")))
    monkeypatch.setenv("VAPID_PUBLIC_KEY", notify._b64(
        key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)))
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:owner@example.com")
    assert notify.push_problem() == ""

    token = notify._vapid_header(ENDPOINT, AFTERNOON).removeprefix("vapid t=").split(", k=")[0]
    signed, signature = token.rsplit(".", 1)
    raw = notify._decode_key(signature)
    key.public_key().verify(
        utils.encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
        signed.encode(), ec.ECDSA(hashes.SHA256()))      # raises if the signature is wrong
    claims = json.loads(notify._decode_key(signed.split(".")[1]))
    assert claims["aud"] == "https://fcm.googleapis.com"      # bound to this push service only
    assert claims["sub"] == "mailto:owner@example.com"


def test_push_cannot_be_turned_on_while_it_could_not_notify_anybody(signed_in_api):
    response = signed_in_api.post("/api/notify/channels", json={"push": True})
    assert response.status_code == 400
    assert "VAPID" in response.json()["detail"]
    assert notify.channels()["push"] is False


# ------------------------------------------------------------ switches ----

def test_both_routes_start_off(signed_in_api):
    body = signed_in_api.get("/api/notify").json()
    assert body["push"]["enabled"] is False
    assert body["self_message"]["enabled"] is False


def test_the_switches_are_read_and_changed_through_the_route(signed_in_api):
    on = signed_in_api.post("/api/notify/channels", json={"self_message": True}).json()
    assert on["self_message"]["enabled"] is True
    assert signed_in_api.get("/api/notify").json()["self_message"]["enabled"] is True
    off = signed_in_api.post("/api/notify/channels", json={"self_message": False}).json()
    assert off["self_message"]["enabled"] is False


def test_a_draft_notifies_nobody_until_a_route_is_turned_on(fresh_db, monkeypatch):
    nudges = planned_nudges(monkeypatch)
    fresh_db.add_held(CHAT, "uko wapi", "niko njiani")
    notify.notify_draft_waiting(CHAT, "Mom", now=AFTERNOON)
    assert nudges == []


# ------------------------------------------------------- subscriptions ----

def test_a_browser_that_subscribes_twice_is_stored_once(signed_in_api):
    assert signed_in_api.post("/api/notify/subscribe", json=SUBSCRIPTION).json() == {
        "added": True, "devices": 1}
    assert signed_in_api.post("/api/notify/subscribe", json=SUBSCRIPTION).json() == {
        "added": False, "devices": 1}
    other = {**SUBSCRIPTION, "endpoint": OTHER_ENDPOINT}
    assert signed_in_api.post("/api/notify/subscribe", json=other).json()["devices"] == 2


def test_an_endpoint_that_is_not_an_https_url_is_refused(signed_in_api):
    for bad in ("http://fcm.googleapis.com/fcm/send/x", "file:///etc/passwd", "not a url"):
        assert signed_in_api.post(
            "/api/notify/subscribe", json={**SUBSCRIPTION, "endpoint": bad}).status_code == 422
    assert notify.device_count() == 0


def test_unsubscribing_stops_delivery(signed_in_api, monkeypatch):
    posted = recording_push(monkeypatch)
    signed_in_api.post("/api/notify/subscribe", json=SUBSCRIPTION)
    nudge = notify.Nudge(push=True, self_message=False, count=1, text="waiting")

    assert notify._deliver(nudge, AFTERNOON)["push_sent"] == 1
    assert posted == [ENDPOINT]

    assert signed_in_api.post("/api/notify/unsubscribe",
                              json={"endpoint": ENDPOINT}).json() == {"removed": True, "devices": 0}
    posted.clear()
    assert notify._deliver(nudge, AFTERNOON)["push_sent"] == 0
    assert posted == []


def test_a_push_service_that_says_the_browser_is_gone_forgets_it(signed_in_api, monkeypatch):
    recording_push(monkeypatch, status=410)
    signed_in_api.post("/api/notify/subscribe", json=SUBSCRIPTION)
    result = notify._deliver(notify.Nudge(push=True, self_message=False, count=1, text="waiting"),
                             AFTERNOON)
    assert result["push_forgotten"] == 1 and result["push_sent"] == 0
    assert notify.device_count() == 0


def test_nothing_is_posted_to_a_push_service_while_push_is_unconfigured(signed_in_api, monkeypatch):
    posted: list[str] = []
    monkeypatch.setattr(notify, "_post_push", lambda endpoint, headers: posted.append(endpoint))
    signed_in_api.post("/api/notify/subscribe", json=SUBSCRIPTION)
    result = notify._deliver(notify.Nudge(push=True, self_message=False, count=1, text="waiting"),
                             AFTERNOON)
    assert posted == [] and result["push_sent"] == 0


# --------------------------------------------- the message to himself ----

def test_three_drafts_in_a_row_send_one_message_rather_than_three(fresh_db, monkeypatch):
    nudges = planned_nudges(monkeypatch)
    notify.set_channels(self_message=True)
    for index in range(3):
        fresh_db.add_held(f"25470000000{index}@c.us", "uko wapi", "niko njiani")
        notify.notify_draft_waiting(f"25470000000{index}@c.us", "Mom", now=AFTERNOON + index)
    assert len(nudges) == 1
    assert nudges[0].count == 1          # the first draft, told at once


def test_the_message_after_the_window_carries_the_count_not_the_last_name(fresh_db, monkeypatch):
    monkeypatch.setenv("NOTIFY_SELF_MIN_MINUTES", "30")
    nudges = planned_nudges(monkeypatch)
    notify.set_channels(self_message=True)

    fresh_db.add_held(CHAT, "uko wapi", "niko njiani")
    notify.notify_draft_waiting(CHAT, "Mom", now=AFTERNOON)
    for index in (2, 3):
        fresh_db.add_held(f"25470000000{index}@c.us", "sasa", "poa")
        notify.notify_draft_waiting(f"25470000000{index}@c.us", "Lulu", now=AFTERNOON + index)

    assert len(nudges) == 1 and nudges[0].text == "A reply to Mom is waiting for you in the dashboard."
    notify.notify_draft_waiting(CHAT, "Lulu", now=AFTERNOON + 31 * 60)
    assert len(nudges) == 2
    assert nudges[1].text == "3 replies are waiting for you in the dashboard."


def test_nobody_is_interrupted_for_a_queue_that_has_been_worked(fresh_db, monkeypatch):
    nudges = planned_nudges(monkeypatch)
    notify.set_channels(self_message=True, push=True)
    notify.notify_draft_waiting(CHAT, "Mom", now=AFTERNOON)      # nothing pending
    assert nudges == []


def test_the_line_goes_to_the_owners_own_chat(fresh_db, monkeypatch):
    payloads = recording_bridge(monkeypatch)
    notify.set_channels(self_message=True)
    fresh_db.add_held(CHAT, "uko wapi", "niko njiani")
    result = notify._deliver(
        notify.Nudge(push=False, self_message=True, count=1,
                     text=notify.waiting_line(1, "Mom")), AFTERNOON)
    assert result["self_message_sent"] is True
    assert payloads == [{"text": "A reply to Mom is waiting for you in the dashboard."}]


def test_a_display_name_is_flattened_to_one_bounded_line():
    assert notify._short_name(" Mom \n  Dearest ") == "Mom Dearest"
    assert notify._short_name("Zero\u0007Width") == "ZeroWidth"
    assert len(notify._short_name("x" * 200)) == notify.MAX_NAME_LENGTH


def test_a_bridge_that_cannot_be_reached_does_not_reach_the_caller(fresh_db, monkeypatch):
    def unreachable(path, payload, timeout):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(notify.bridge, "post_sync", unreachable)
    monkeypatch.setattr(notify, "_dispatch", lambda nudge, now: notify._deliver(nudge, now))
    notify.set_channels(self_message=True)
    fresh_db.add_held(CHAT, "uko wapi", "niko njiani")

    result = notify._deliver(notify.Nudge(push=False, self_message=True, count=1, text="waiting"),
                             AFTERNOON)
    assert result["self_message_sent"] is False
    notify.notify_draft_waiting(CHAT, "Mom", now=AFTERNOON)      # must not raise


def test_a_bridge_that_refuses_the_message_is_reported_as_not_sent(fresh_db, monkeypatch):
    recording_bridge(monkeypatch, status=503)
    result = notify._deliver(notify.Nudge(push=False, self_message=True, count=2, text="waiting"),
                             AFTERNOON)
    assert result["self_message_sent"] is False


# --------------------------------------------------------- quiet hours ----

def test_quiet_hours_suppress_every_route(fresh_db, monkeypatch):
    monkeypatch.setenv("QUIET_HOURS", "22:00-07:00")
    nudges = planned_nudges(monkeypatch)
    notify.set_channels(push=True, self_message=True)
    fresh_db.add_held(CHAT, "uko wapi", "niko njiani")

    notify.notify_draft_waiting(CHAT, "Mom", now=MIDDLE_OF_THE_NIGHT)
    assert nudges == []

    notify.notify_draft_waiting(CHAT, "Mom", now=AFTERNOON)
    assert len(nudges) == 1 and nudges[0].push and nudges[0].self_message


def test_the_quiet_window_is_reported_to_the_page(signed_in_api, monkeypatch):
    monkeypatch.setenv("QUIET_HOURS", "22:00-07:00")
    assert signed_in_api.get("/api/notify").json()["quiet_hours"]["window"] == "22:00-07:00"


# ---------------------------------------------------------------- test ----

def test_a_test_notification_needs_a_route_turned_on_first(signed_in_api):
    assert signed_in_api.post("/api/notify/test").status_code == 400


def test_a_test_notification_goes_out_even_in_quiet_hours(signed_in_api, monkeypatch):
    monkeypatch.setenv("QUIET_HOURS", "00:00-23:59")
    payloads = recording_bridge(monkeypatch)
    notify.set_channels(self_message=True)

    result = signed_in_api.post("/api/notify/test").json()
    assert result["self_message_sent"] is True
    assert "notifications are working" in payloads[0]["text"]


def test_a_test_notification_does_not_spend_the_rate_limit_window(signed_in_api, fresh_db, monkeypatch):
    recording_bridge(monkeypatch)
    notify.set_channels(self_message=True)
    signed_in_api.post("/api/notify/test")

    nudges = planned_nudges(monkeypatch)
    fresh_db.add_held(CHAT, "uko wapi", "niko njiani")
    notify.notify_draft_waiting(CHAT, "Mom", now=AFTERNOON)
    assert len(nudges) == 1


# ---------------------------------------------------------- auth, logs ----

@pytest.mark.parametrize("method, path, body", [
    ("get", "/api/notify", None),
    ("post", "/api/notify/channels", {"self_message": True}),
    ("post", "/api/notify/subscribe", SUBSCRIPTION),
    ("post", "/api/notify/unsubscribe", {"endpoint": ENDPOINT}),
    ("post", "/api/notify/test", None),
])
def test_every_route_needs_a_signed_in_session(api, method, path, body):
    call = getattr(api, method)
    response = call(path) if body is None else call(path, json=body)
    assert response.status_code == 401
    assert notify.device_count() == 0


def test_the_service_worker_is_served_from_the_root_so_its_scope_covers_the_dashboard(api):
    response = api.get("/sw.js")        # no session: a worker script is fetched before login
    assert response.status_code == 200
    assert response.headers["service-worker-allowed"] == "/"
    assert "javascript" in response.headers["content-type"]
    assert "pushsubscriptionchange" in response.text


def test_no_draft_text_name_or_endpoint_reaches_the_logs(signed_in_api, fresh_db, monkeypatch):
    log = Path(os.environ["APP_LOG_FILE"])
    already = log.read_text(encoding="utf-8") if log.exists() else ""
    recording_push(monkeypatch)
    payloads = recording_bridge(monkeypatch)
    monkeypatch.setattr(notify, "_dispatch", lambda nudge, now: notify._deliver(nudge, now))

    signed_in_api.post("/api/notify/subscribe", json=SUBSCRIPTION)
    notify.set_channels(push=True, self_message=True)
    fresh_db.add_held(CHAT, "uko wapi tafadhali", "niko njiani bwana")
    notify.notify_draft_waiting(CHAT, "Mom", now=AFTERNOON)

    assert payloads == [{"text": "A reply to Mom is waiting for you in the dashboard."}]
    written = log.read_text(encoding="utf-8")[len(already):]
    assert "notify_sent" in written          # it was logged at all
    for secret in (ENDPOINT, "abc123-this-one-browser", "fcm.googleapis.com",
                   "uko wapi tafadhali", "niko njiani bwana", "254700000001", "Mom"):
        assert secret not in written
