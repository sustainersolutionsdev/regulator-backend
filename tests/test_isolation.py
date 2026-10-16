import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

import uuid
import httpx
from core.get_test_token import get_id_token

BASE_URL = "http://127.0.0.1:8000"
# httpx defaults to 5s. The first request after a server start pays for
# Firebase cert fetch + Firestore channel setup and can exceed that.
TIMEOUT = 30.0

TEST_PASSWORD = os.environ["TEST_ACCOUNTS_PASSWORD"].strip()

TENANT_A_ADMIN = ("admin@testtenant.com", TEST_PASSWORD)
TENANT_B_ADMIN = ("admin@tenantb.com", TEST_PASSWORD)
TENANT_A_USER = ("user@testtenant.com", TEST_PASSWORD)  # BU1 only

# T020: the Business Unit-scoped write tests below use the real write path,
# PATCH /regulations/{id}/bu-status/{bu_code}, instead of the removed
# test-only /test-write endpoint. Titles carry this prefix so
# tests/cleanup_test_data.py removes them after the session.
TITLE_PREFIX = "T020 Isolation Regulation "


def auth_header(email, password):
    token = get_id_token(email, password)
    return {"Authorization": f"Bearer {token}"}


def shared_regulation_id() -> str:
    """A regulation tagged to BU1 and BU2, created by Tenant A's admin."""
    resp = httpx.post(
        f"{BASE_URL}/regulations",
        json={
            "title": f"{TITLE_PREFIX}{uuid.uuid4().hex[:8]}",
            "due_date": "2027-06-30",
            "domain": "Batteries",
            "business_unit_ids": ["BU1", "BU2"],
            "add_to_roadmap_dashboard": False,
        },
        headers=auth_header(*TENANT_A_ADMIN),
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def set_bu_status(reg_id, bu_code, headers):
    return httpx.patch(
        f"{BASE_URL}/regulations/{reg_id}/bu-status/{bu_code}",
        json={"status": "pending"},
        headers=headers,
        timeout=TIMEOUT,
    )


def test_tenant_a_admin_sees_only_tenant_a_users():
    headers = auth_header(*TENANT_A_ADMIN)
    resp = httpx.get(f"{BASE_URL}/users", headers=headers, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    users = resp.json()
    assert len(users) > 0
    for u in users:
        assert "tenantb.com" not in u.get("email", ""), \
            f"LEAK: Tenant A admin saw a Tenant B record: {u}"


def test_tenant_b_admin_sees_only_tenant_b_users():
    headers = auth_header(*TENANT_B_ADMIN)
    resp = httpx.get(f"{BASE_URL}/users", headers=headers, timeout=TIMEOUT)
    assert resp.status_code == 200
    users = resp.json()
    assert len(users) > 0
    for u in users:
        assert "testtenant.com" not in u.get("email", ""), \
            f"LEAK: Tenant B admin saw a Tenant A record: {u}"


def test_tenant_b_admin_zero_tenant_a_records():
    """The explicit zero-leak assertion the TAD isolation suite calls for."""
    headers = auth_header(*TENANT_B_ADMIN)
    resp = httpx.get(f"{BASE_URL}/users", headers=headers, timeout=TIMEOUT)
    tenant_a_emails = {"admin@testtenant.com", "user@testtenant.com"}
    returned_emails = {u.get("email") for u in resp.json()}
    assert tenant_a_emails.isdisjoint(returned_emails)


def test_no_token_rejected():
    resp = httpx.get(f"{BASE_URL}/me", timeout=TIMEOUT)
    assert resp.status_code == 401


def test_user_role_blocked_from_out_of_scope_bu():
    reg_id = shared_regulation_id()
    resp = set_bu_status(reg_id, "BU2", auth_header(*TENANT_A_USER))
    assert resp.status_code == 403
    assert "not permitted" in resp.json()["detail"]


def test_user_role_allowed_in_own_bu():
    reg_id = shared_regulation_id()
    resp = set_bu_status(reg_id, "BU1", auth_header(*TENANT_A_USER))
    assert resp.status_code == 200, resp.text


def test_admin_bypasses_bu_restriction():
    reg_id = shared_regulation_id()
    resp = set_bu_status(reg_id, "BU2", auth_header(*TENANT_A_ADMIN))
    assert resp.status_code == 200, resp.text


def test_test_write_endpoint_removed():
    """T020: the test-only endpoint is gone from the app."""
    resp = httpx.post(f"{BASE_URL}/test-write/BU1", headers=auth_header(*TENANT_A_ADMIN), timeout=TIMEOUT)
    assert resp.status_code in (404, 405)
