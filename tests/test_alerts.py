"""
T025 - email alerts against the running server (FR-3.1, DL-9).

  - GET/PUT /me/email-alerts: each person's own on/off setting, on by default.
  - POST /regulations writes one Alerts Log entry after the response when the
    regulation is on the Roadmap/Dashboard, and none when it isn't.
  - A user who turned alerts off is recorded as skipped.

The server must run with ALERTS_MODE unset or "log" (the default, and what
Cloud Build uses), so these tests never send an email. Test regulations use
the "T025 Alert Regulation " prefix; cleanup removes them and their Alerts
Log entries. The fixed test accounts are always left with alerts ON.
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

import time
import uuid
import httpx
import pytest
from core.get_test_token import get_id_token
from core.deps import db

BASE_URL = "http://127.0.0.1:8000"
TIMEOUT = 30.0
TENANT = "test-tenant-001"
WAIT_SECONDS = 15

TEST_PASSWORD = os.environ["TEST_ACCOUNTS_PASSWORD"].strip()
ADMIN = ("admin@testtenant.com", TEST_PASSWORD)
USER = ("user@testtenant.com", TEST_PASSWORD)


def auth_header(email, password):
    return {"Authorization": f"Bearer {get_id_token(email, password)}"}


def set_alerts(headers, enabled):
    return httpx.put(f"{BASE_URL}/me/email-alerts", json={"enabled": enabled}, headers=headers, timeout=TIMEOUT)


def get_alerts(headers):
    return httpx.get(f"{BASE_URL}/me/email-alerts", headers=headers, timeout=TIMEOUT)


def uid_of(headers):
    return httpx.get(f"{BASE_URL}/me", headers=headers, timeout=TIMEOUT).json()["uid"]


def create_regulation(headers, on_dashboard=True):
    payload = {
        "title": f"T025 Alert Regulation {uuid.uuid4().hex[:8]}",
        "abbreviated_title": "T025 Alert",
        "due_date": "2027-02-18",
        "domain": "Batteries",
        "business_unit_ids": ["BU1"],
        "add_to_roadmap_dashboard": on_dashboard,
    }
    r = httpx.post(f"{BASE_URL}/regulations", json=payload, headers=headers, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def alert_entries(reg_id):
    q = db.collection("tenants").document(TENANT).collection("alertsLog").where("regulationId", "==", reg_id)
    return [d.to_dict() for d in q.stream()]


def wait_for_entry(reg_id):
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        entries = alert_entries(reg_id)
        if entries:
            return entries
        time.sleep(1)
    return []


@pytest.fixture
def admin():
    h = auth_header(*ADMIN)
    yield h
    set_alerts(h, True)


@pytest.fixture
def user():
    h = auth_header(*USER)
    yield h
    set_alerts(h, True)


# --- Email alerts on/off (DL-9) ---------------------------------------------------

def test_email_alerts_setting_requires_token():
    assert httpx.get(f"{BASE_URL}/me/email-alerts", timeout=TIMEOUT).status_code == 401
    assert httpx.put(f"{BASE_URL}/me/email-alerts", json={"enabled": False}, timeout=TIMEOUT).status_code == 401


def test_email_alerts_round_trip(admin):
    assert set_alerts(admin, False).json() == {"enabled": False}
    assert get_alerts(admin).json() == {"enabled": False}
    assert set_alerts(admin, True).json() == {"enabled": True}
    assert get_alerts(admin).json() == {"enabled": True}


def test_each_person_has_their_own_setting(admin, user):
    set_alerts(admin, False)
    set_alerts(user, True)
    assert get_alerts(admin).json()["enabled"] is False
    assert get_alerts(user).json()["enabled"] is True


def test_setting_must_be_true_or_false(admin):
    assert set_alerts(admin, "no").status_code == 422
    assert httpx.put(f"{BASE_URL}/me/email-alerts", json={}, headers=admin, timeout=TIMEOUT).status_code == 422


def test_setting_does_not_touch_the_saved_dashboard_view(admin):
    httpx.put(f"{BASE_URL}/me/dashboard-view", json={"view": "view=bu"}, headers=admin, timeout=TIMEOUT)
    set_alerts(admin, False)
    view = httpx.get(f"{BASE_URL}/me/dashboard-view", headers=admin, timeout=TIMEOUT).json()["view"]
    assert view == "view=bu"


# --- New-regulation alert (FR-3.1) ------------------------------------------------

def test_new_regulation_on_dashboard_writes_one_alert_entry(admin):
    reg_id = create_regulation(admin)
    entries = wait_for_entry(reg_id)
    assert len(entries) == 1, "no Alerts Log entry within the wait"
    entry = entries[0]
    assert entry["type"] == "regulation_added"
    assert entry["triggeredBy"] == uid_of(admin)
    assert entry["status"] in ("logged", "sent", "no_recipients"), entry
    assert uid_of(admin) in entry["recipientUids"]
    assert entry["subject"] == "[Regulator] New regulation: T025 Alert (due 18 Feb 2027)"


def test_no_alert_for_regulation_not_on_dashboard(admin):
    reg_id = create_regulation(admin, on_dashboard=False)
    time.sleep(5)
    assert alert_entries(reg_id) == []


def test_user_with_alerts_off_is_skipped(admin, user):
    # Precondition: the fixed User test account must be an active directory
    # record. If it was soft-removed (hidden), alerts skip it as removed before
    # the on/off setting is read (found Oct 20 2026; repaired by
    # tools/fix_t025_restore_test_user.py).
    record = db.collection("tenants").document(TENANT).collection("users").document(uid_of(user)).get()
    assert record.exists and not (record.to_dict() or {}).get("hidden"), (
        "user@testtenant.com's directory record is missing or hidden - run "
        "python tools\\fix_t025_restore_test_user.py --apply"
    )
    set_alerts(user, False)
    reg_id = create_regulation(admin)
    entries = wait_for_entry(reg_id)
    assert len(entries) == 1
    user_uid = uid_of(user)
    assert user_uid in entries[0]["skippedOptedOut"]
    assert user_uid not in entries[0]["recipientUids"]
    assert user_uid not in entries[0].get("skippedRemoved", [])


def test_other_tenant_users_are_never_recipients(admin):
    other = auth_header("admin@tenantb.com", TEST_PASSWORD)
    reg_id = create_regulation(admin)
    entries = wait_for_entry(reg_id)
    assert len(entries) == 1
    assert uid_of(other) not in entries[0]["recipientUids"]
