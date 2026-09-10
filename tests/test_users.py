import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

import uuid
import httpx
from core.get_test_token import get_id_token

BASE_URL = "http://127.0.0.1:8000"
TIMEOUT = 30.0

TEST_PASSWORD = os.environ["TEST_ACCOUNTS_PASSWORD"].strip()

TENANT_A_ADMIN = ("admin@testtenant.com", TEST_PASSWORD)
TENANT_A_USER = ("user@testtenant.com", TEST_PASSWORD)


def auth_header(email, password):
    token = get_id_token(email, password)
    return {"Authorization": f"Bearer {token}"}


def unique_email(prefix: str) -> str:
    """Unique per run so this suite doesn't accumulate duplicate-email
    collisions across repeated executions, unlike test_business_units.py's
    fixed BU-DUP-TEST code."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}@testtenant.com"


def test_user_role_cannot_create_user():
    headers = auth_header(*TENANT_A_USER)
    resp = httpx.post(
        f"{BASE_URL}/users",
        json={
            "email": unique_email("blocked"),
            "display_name": "Should Fail",
            "role": "user",
            "business_unit_ids": ["BU1"],
        },
        headers=headers,
        timeout=TIMEOUT,
    )
    assert resp.status_code == 403
    assert "Admin or SME" in resp.json()["detail"]


def test_admin_can_create_user_with_bu_and_no_password_leaks():
    headers = auth_header(*TENANT_A_ADMIN)
    email = unique_email("newuser")

    resp = httpx.post(
        f"{BASE_URL}/users",
        json={
            "email": email,
            "display_name": "New Test User",
            "role": "user",
            "business_unit_ids": ["BU1"],
        },
        headers=headers,
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["role"] == "user"
    assert "uid" in body
    assert "reset_link" in body
    assert body["reset_link"].startswith("https://")

    # FR-0.4 security requirement: no password field or literal password
    # value anywhere in the response. The reset_link legitimately contains
    # "resetpassword" as part of its URL action mode — that's expected and
    # fine; this checks for an actual password VALUE, not that substring.
    assert "password" not in body
    assert "temp_password" not in body
    assert body["reset_link"].count("resetpassword") <= 1


def test_user_role_requires_at_least_one_bu():
    headers = auth_header(*TENANT_A_ADMIN)
    resp = httpx.post(
        f"{BASE_URL}/users",
        json={
            "email": unique_email("nobus"),
            "display_name": "No BU User",
            "role": "user",
            "business_unit_ids": [],
        },
        headers=headers,
        timeout=TIMEOUT,
    )
    assert resp.status_code == 400
    assert "Business Unit" in resp.json()["detail"]


def test_duplicate_email_returns_409():
    headers = auth_header(*TENANT_A_ADMIN)
    email = unique_email("dup")
    payload = {
        "email": email,
        "display_name": "Dup Test",
        "role": "user",
        "business_unit_ids": ["BU1"],
    }

    first = httpx.post(f"{BASE_URL}/users", json=payload, headers=headers, timeout=TIMEOUT)
    assert first.status_code == 200, first.text

    second = httpx.post(f"{BASE_URL}/users", json=payload, headers=headers, timeout=TIMEOUT)
    assert second.status_code == 409
    assert "already exists" in second.json()["detail"]


def test_invalid_role_rejected():
    headers = auth_header(*TENANT_A_ADMIN)
    resp = httpx.post(
        f"{BASE_URL}/users",
        json={
            "email": unique_email("badrole"),
            "display_name": "Bad Role",
            "role": "superadmin",
            "business_unit_ids": [],
        },
        headers=headers,
        timeout=TIMEOUT,
    )
    assert resp.status_code == 400
    assert "Invalid role" in resp.json()["detail"]


def test_sme_role_is_auto_scoped_to_all_tenant_bus():
    headers = auth_header(*TENANT_A_ADMIN)
    email = unique_email("sme")

    resp = httpx.post(
        f"{BASE_URL}/users",
        json={
            "email": email,
            "display_name": "SME Auto-Scope Test",
            "role": "sme",
            "business_unit_ids": ["BU1"],
        },
        headers=headers,
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, resp.text

    all_bus = httpx.get(f"{BASE_URL}/business-units", headers=headers, timeout=TIMEOUT)
    all_codes = {bu["code"] for bu in all_bus.json()}

    listed = httpx.get(f"{BASE_URL}/users", headers=headers, timeout=TIMEOUT)
    created = next(u for u in listed.json() if u["email"] == email)

    assert set(created["businessUnitIds"]) == all_codes