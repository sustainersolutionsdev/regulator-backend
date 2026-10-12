"""
T011 - Regulation Roadmap read path: GET /regulations (FR-1.1, FR-1.4, NFR-1).

Also closes the T009 carried-forward item "cross-tenant read test for
regulations: deferred until GET /regulations exists (T011/T017)".

Covers:
  - no token -> 401
  - a regulation created through POST /regulations comes back on the
    read path with server-generated id and the Roadmap fields
  - User role can VIEW tenant-wide, including a Business Unit it cannot
    edit (FR-1.4: viewing is tenant-wide for every role)
  - Tenant A and Tenant B never see each other's regulations (NFR-1)
  - soft-deleted (hidden) regulations are excluded (NFR-8)
  - results are sorted by due date
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

import uuid
import httpx
from core.get_test_token import get_id_token
from core.tenancy import db

BASE_URL = "http://127.0.0.1:8000"
TIMEOUT = 30.0

TEST_PASSWORD = os.environ["TEST_ACCOUNTS_PASSWORD"].strip()

TENANT_A_ADMIN = ("admin@testtenant.com", TEST_PASSWORD)
TENANT_A_USER = ("user@testtenant.com", TEST_PASSWORD)  # BU1 only
TENANT_B_ADMIN = ("admin@tenantb.com", TEST_PASSWORD)

TENANT_A_ID = "test-tenant-001"


def auth_header(email, password):
    token = get_id_token(email, password)
    return {"Authorization": f"Bearer {token}"}


def create_regulation(headers, **overrides) -> dict:
    payload = {
        "title": f"T011 Roadmap Regulation {uuid.uuid4().hex[:8]}",
        "abbreviated_title": "T011 TEST",
        "due_date": "2027-06-30",
        "domain": "Batteries",
        "business_unit_ids": ["BU1"],
        # False by default so repeated runs don't flood the live Roadmap on
        # test-tenant-001; the field test below sets it True explicitly.
        "add_to_roadmap_dashboard": False,
    }
    payload.update(overrides)
    resp = httpx.post(f"{BASE_URL}/regulations", json=payload, headers=headers, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    return resp.json()


def list_regulations(headers) -> list[dict]:
    resp = httpx.get(f"{BASE_URL}/regulations", headers=headers, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_get_regulations_rejects_missing_token():
    resp = httpx.get(f"{BASE_URL}/regulations", timeout=TIMEOUT)
    assert resp.status_code == 401


def test_created_regulation_is_returned_with_roadmap_fields():
    headers = auth_header(*TENANT_A_ADMIN)
    created = create_regulation(headers, add_to_roadmap_dashboard=True)

    match = next((r for r in list_regulations(headers) if r["id"] == created["id"]), None)
    assert match is not None, "regulation created via POST /regulations missing from GET /regulations"
    assert match["title"] == created["title"]
    assert match["abbreviatedTitle"] == "T011 TEST"
    assert match["dueDate"] == "2027-06-30"
    assert match["addToRoadmapDashboard"] is True
    assert match["businessUnitIds"] == ["BU1"]


def test_user_role_views_regulations_outside_own_business_unit():
    """FR-1.4: viewing is tenant-wide; only editing is BU-scoped."""
    created = create_regulation(auth_header(*TENANT_A_ADMIN), business_unit_ids=["BU2"])

    ids = {r["id"] for r in list_regulations(auth_header(*TENANT_A_USER))}
    assert created["id"] in ids


def test_regulations_never_cross_tenants():
    a_headers = auth_header(*TENANT_A_ADMIN)
    b_headers = auth_header(*TENANT_B_ADMIN)
    a_reg = create_regulation(a_headers)
    b_reg = create_regulation(b_headers)

    a_ids = {r["id"] for r in list_regulations(a_headers)}
    b_ids = {r["id"] for r in list_regulations(b_headers)}

    assert a_reg["id"] in a_ids and b_reg["id"] in b_ids
    assert a_reg["id"] not in b_ids, "LEAK: Tenant B saw a Tenant A regulation"
    assert b_reg["id"] not in a_ids, "LEAK: Tenant A saw a Tenant B regulation"


def test_hidden_regulation_is_excluded():
    """NFR-8 soft delete. No hide endpoint exists yet, so hide it directly."""
    headers = auth_header(*TENANT_A_ADMIN)
    created = create_regulation(headers)

    (
        db.collection("tenants").document(TENANT_A_ID)
        .collection("regulations").document(created["id"])
        .update({"hidden": True})
    )

    ids = {r["id"] for r in list_regulations(headers)}
    assert created["id"] not in ids


def test_regulations_sorted_by_due_date():
    headers = auth_header(*TENANT_A_ADMIN)
    create_regulation(headers, due_date="2029-01-15")
    create_regulation(headers, due_date="2026-11-01")

    due_dates = [r["dueDate"] for r in list_regulations(headers) if r.get("dueDate")]
    assert due_dates == sorted(due_dates)
