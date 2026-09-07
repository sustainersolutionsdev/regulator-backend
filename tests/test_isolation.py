import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

import httpx
from core.get_test_token import get_id_token

BASE_URL = "http://127.0.0.1:8000"

TENANT_A_ADMIN = ("admin@testtenant.com", "TempPassword123!")
TENANT_B_ADMIN = ("admin@tenantb.com", "TempPassword123!")
TENANT_A_USER = ("user@testtenant.com", "TempPassword123!")


def auth_header(email, password):
    token = get_id_token(email, password)
    return {"Authorization": f"Bearer {token}"}


def test_tenant_a_admin_sees_only_tenant_a_users():
    headers = auth_header(*TENANT_A_ADMIN)
    resp = httpx.get(f"{BASE_URL}/users", headers=headers)
    assert resp.status_code == 200
    users = resp.json()
    assert len(users) > 0
    for u in users:
        assert "tenantb.com" not in u.get("email", ""), \
            f"LEAK: Tenant A admin saw a Tenant B record: {u}"


def test_tenant_b_admin_sees_only_tenant_b_users():
    headers = auth_header(*TENANT_B_ADMIN)
    resp = httpx.get(f"{BASE_URL}/users", headers=headers)
    assert resp.status_code == 200
    users = resp.json()
    assert len(users) > 0
    for u in users:
        assert "testtenant.com" not in u.get("email", ""), \
            f"LEAK: Tenant B admin saw a Tenant A record: {u}"


def test_tenant_b_admin_zero_tenant_a_records():
    """The explicit zero-leak assertion the TAD isolation suite calls for."""
    headers = auth_header(*TENANT_B_ADMIN)
    resp = httpx.get(f"{BASE_URL}/users", headers=headers)
    tenant_a_emails = {"admin@testtenant.com", "user@testtenant.com"}
    returned_emails = {u.get("email") for u in resp.json()}
    assert tenant_a_emails.isdisjoint(returned_emails)


def test_no_token_rejected():
    resp = httpx.get(f"{BASE_URL}/me")
    assert resp.status_code == 401


def test_user_role_blocked_from_out_of_scope_bu():
    headers = auth_header(*TENANT_A_USER)
    resp = httpx.post(f"{BASE_URL}/test-write/BU2", headers=headers)
    assert resp.status_code == 403
    assert "not permitted" in resp.json()["detail"]


def test_user_role_allowed_in_own_bu():
    headers = auth_header(*TENANT_A_USER)
    resp = httpx.post(f"{BASE_URL}/test-write/BU1", headers=headers)
    assert resp.status_code == 200


def test_admin_bypasses_bu_restriction():
    headers = auth_header(*TENANT_A_ADMIN)
    resp = httpx.post(f"{BASE_URL}/test-write/BU2", headers=headers)
    assert resp.status_code == 200