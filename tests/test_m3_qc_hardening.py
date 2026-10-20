"""
T027 - Milestone 3 internal QC: alert paths that must stay silent.

  - A User's attempt to edit main details is refused (403, DL-6) and sends no
    "updated" email.
  - Another tenant's Admin cannot edit the regulation (404, NFR-1), so no
    email is sent to either tenant.
  - Removing a regulation sends no email (DL-9, 3A).
  - The User's own Business Unit status update sends no email (DL-9, 1A).

Regulations use the registered "T025 Alert Regulation " prefix, so the session
cleanup removes them and their Alerts Log entries. The server must run with
ALERTS_MODE=log (the default).
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

import time
import uuid
import httpx
from core.get_test_token import get_id_token
from core.deps import db

BASE_URL = "http://127.0.0.1:8000"
TIMEOUT = 30.0
TENANT = "test-tenant-001"
QUIET_SECONDS = 5

TEST_PASSWORD = os.environ["TEST_ACCOUNTS_PASSWORD"].strip()


def auth_header(email):
    return {"Authorization": f"Bearer {get_id_token(email, TEST_PASSWORD)}"}


def create_on_dashboard(headers):
    payload = {
        "title": f"T025 Alert Regulation {uuid.uuid4().hex[:8]}",
        "abbreviated_title": "T027 QC",
        "due_date": "2027-02-18",
        "domain": "Batteries",
        "business_unit_ids": ["BU1"],
        "add_to_roadmap_dashboard": True,
    }
    r = httpx.post(f"{BASE_URL}/regulations", json=payload, headers=headers, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def entries(reg_id, alert_type):
    q = db.collection("tenants").document(TENANT).collection("alertsLog").where("regulationId", "==", reg_id)
    return [d.to_dict() for d in q.stream() if d.to_dict().get("type") == alert_type]


def test_user_edit_is_refused_and_sends_nothing():
    reg_id = create_on_dashboard(auth_header("admin@testtenant.com"))
    r = httpx.patch(f"{BASE_URL}/regulations/{reg_id}", json={"title": "User tried this"},
                    headers=auth_header("user@testtenant.com"), timeout=TIMEOUT)
    assert r.status_code == 403
    time.sleep(QUIET_SECONDS)
    assert entries(reg_id, "regulation_updated") == []


def test_other_tenant_cannot_edit_and_nothing_is_sent():
    reg_id = create_on_dashboard(auth_header("admin@testtenant.com"))
    r = httpx.patch(f"{BASE_URL}/regulations/{reg_id}", json={"due_date": "2028-01-01"},
                    headers=auth_header("admin@tenantb.com"), timeout=TIMEOUT)
    assert r.status_code == 404
    time.sleep(QUIET_SECONDS)
    assert entries(reg_id, "regulation_updated") == []


def test_removing_a_regulation_sends_nothing():
    admin = auth_header("admin@testtenant.com")
    reg_id = create_on_dashboard(admin)
    r = httpx.delete(f"{BASE_URL}/regulations/{reg_id}", headers=admin, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    time.sleep(QUIET_SECONDS)
    assert entries(reg_id, "regulation_updated") == []
    assert len(entries(reg_id, "regulation_added")) <= 1  # only the original "added" alert


def test_user_status_update_sends_nothing():
    reg_id = create_on_dashboard(auth_header("admin@testtenant.com"))
    r = httpx.patch(f"{BASE_URL}/regulations/{reg_id}/bu-status/BU1", json={"status": "action_needed", "note": "T027"},
                    headers=auth_header("user@testtenant.com"), timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    time.sleep(QUIET_SECONDS)
    assert entries(reg_id, "regulation_updated") == []
