import sys
import os
import uuid
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

import httpx
from core.get_test_token import get_id_token

BASE_URL = "http://127.0.0.1:8000"

TEST_PASSWORD = os.environ["TEST_ACCOUNTS_PASSWORD"]

TENANT_A_ADMIN = ("admin@testtenant.com", TEST_PASSWORD)
TENANT_A_USER = ("user@testtenant.com", TEST_PASSWORD)
TENANT_B_ADMIN = ("admin@tenantb.com", TEST_PASSWORD)


def auth_header(email, password):
    token = get_id_token(email, password)
    return {"Authorization": f"Bearer {token}"}

def test_any_role_can_list_own_tenant_bus():
    headers = auth_header(*TENANT_A_USER)
    resp = httpx.get(f"{BASE_URL}/business-units", headers=headers)
    assert resp.status_code == 200, resp.text
    codes = {bu["code"] for bu in resp.json()}
    assert "BU1" in codes  # seeded by provision_tenant


def test_tenant_b_admin_does_not_see_tenant_a_bus():
    headers = auth_header(*TENANT_B_ADMIN)
    resp = httpx.get(f"{BASE_URL}/business-units", headers=headers)
    assert resp.status_code == 200
    codes = {bu["code"] for bu in resp.json()}
    assert "BU2" not in codes  # BU2 belongs to Tenant A, created below


def test_user_role_cannot_create_bu():
    headers = auth_header(*TENANT_A_USER)
    resp = httpx.post(
        f"{BASE_URL}/business-units",
        params={"bu_code": "BU-USER-ATTEMPT", "label": "Should Fail"},
        headers=headers,
    )
    assert resp.status_code == 403
    assert "Admin or SME" in resp.json()["detail"]


def test_admin_can_create_bu_and_it_appears_in_list():
    headers = auth_header(*TENANT_A_ADMIN)
    bu_code = f"BU-T006-TEST-{uuid.uuid4().hex[:8]}"

    resp = httpx.post(
        f"{BASE_URL}/business-units",
        params={"bu_code": bu_code, "label": "T006 Test Unit"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    listed = httpx.get(f"{BASE_URL}/business-units", headers=headers)
    codes = {bu["code"] for bu in listed.json()}
    assert bu_code in codes
    

def test_duplicate_bu_code_returns_409():
    headers = auth_header(*TENANT_A_ADMIN)
    httpx.post(
        f"{BASE_URL}/business-units",
        params={"bu_code": "BU-DUP-TEST", "label": "First"},
        headers=headers,
    )
    resp = httpx.post(
        f"{BASE_URL}/business-units",
        params={"bu_code": "BU-DUP-TEST", "label": "Second"},
        headers=headers,
    )
    assert resp.status_code == 409