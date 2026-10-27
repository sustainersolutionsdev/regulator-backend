"""
T061 - invitation email, against the server (local / Cloud Build run with
ALERTS_MODE=log, so nothing is emailed; @testtenant.com is never emailed).

  - POST /users no longer returns a password link and writes a user_invited
    Alerts Log entry for the new person.
  - POST /users/{uid}/resend-invitation: Admin/SME only, own company only.

Users are created with the "newuser-<hex>@testtenant.com" pattern, so the
session cleanup removes them and their invitation log entries.
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
from core.tenancy import db

BASE_URL = "http://127.0.0.1:8000"
TIMEOUT = 30.0
TENANT_A_ID = "test-tenant-001"

TEST_PASSWORD = os.environ["TEST_ACCOUNTS_PASSWORD"].strip()
TENANT_A_ADMIN = ("admin@testtenant.com", TEST_PASSWORD)
TENANT_A_SME = ("sme@testtenant.com", TEST_PASSWORD)
TENANT_A_USER = ("user@testtenant.com", TEST_PASSWORD)
TENANT_B_ADMIN = ("admin@tenantb.com", TEST_PASSWORD)


def auth_header(email, password):
    return {"Authorization": f"Bearer {get_id_token(email, password)}"}


def create_person() -> str:
    resp = httpx.post(
        f"{BASE_URL}/users",
        json={"email": f"newuser-{uuid.uuid4().hex[:8]}@testtenant.com", "display_name": "Invite Test",
              "role": "user", "business_unit_ids": ["BU1"]},
        headers=auth_header(*TENANT_A_ADMIN), timeout=TIMEOUT,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "reset_link" not in body and body["invitation"] == "sent"
    return body["uid"]


def log_entries(uid, entry_type, wait=5.0, at_least=1):
    """The invitation runs just after the response; poll briefly."""
    deadline = time.time() + wait
    while True:
        docs = [d.to_dict() for d in db.collection("tenants").document(TENANT_A_ID)
                .collection("alertsLog").where("userUid", "==", uid).stream()]
        found = [d for d in docs if d.get("type") == entry_type]
        if len(found) >= at_least or time.time() > deadline:
            return found
        time.sleep(0.5)


def resend(uid, headers):
    return httpx.post(f"{BASE_URL}/users/{uid}/resend-invitation", headers=headers, timeout=TIMEOUT)


def test_add_user_writes_an_invitation_entry_without_the_link():
    uid = create_person()
    entries = log_entries(uid, "user_invited")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["subject"].startswith("You've been added to Regulator")
    text = str(entry).lower()
    assert "oobcode" not in text and "@testtenant.com" not in text  # no link, no address


def test_resend_requires_token():
    assert httpx.post(f"{BASE_URL}/users/x/resend-invitation", timeout=TIMEOUT).status_code == 401


def test_admin_and_sme_can_resend():
    uid = create_person()
    for account in (TENANT_A_ADMIN, TENANT_A_SME):
        resp = resend(uid, auth_header(*account))
        assert resp.status_code == 200, resp.text
        assert resp.json()["uid"] == uid
    assert len(log_entries(uid, "invitation_resent", at_least=2)) == 2


def test_user_role_cannot_resend():
    uid = create_person()
    assert resend(uid, auth_header(*TENANT_A_USER)).status_code == 403


def test_other_company_or_unknown_user_is_404():
    uid = create_person()
    assert resend(uid, auth_header(*TENANT_B_ADMIN)).status_code == 404
    assert resend("no-such-user", auth_header(*TENANT_A_ADMIN)).status_code == 404
