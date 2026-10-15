"""
T018 - Compliance status and explanation on PATCH /regulations/{id}.

Rules (FR-2.1, FR-2.5): status is "compliant" / "pending" / "action_needed",
or null for "not set". It is set by a person, never defaulted or inferred,
and its optional explanation is authored by Admin/SME. Every change is
audited (NFR-2) and stays inside the caller's tenant (NFR-1).

Until T020 adds the Business Unit-scoped Dashboard edit for Users, only
Admin and SME can set status; test_user_cannot_set_status_yet records that
and is expected to change with T020.

Every regulation created here is titled "T018 Status Regulation ..." so
tests/cleanup_test_data.py removes it after the session.
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
TITLE_PREFIX = "T018 Status Regulation "

TEST_PASSWORD = os.environ["TEST_ACCOUNTS_PASSWORD"].strip()
TENANT_A_ADMIN = ("admin@testtenant.com", TEST_PASSWORD)
TENANT_A_SME = ("sme@testtenant.com", TEST_PASSWORD)
TENANT_A_USER = ("user@testtenant.com", TEST_PASSWORD)  # BU1 only
TENANT_B_ADMIN = ("admin@tenantb.com", TEST_PASSWORD)
TENANT_A_ID = "test-tenant-001"


def auth_header(email, password):
    return {"Authorization": f"Bearer {get_id_token(email, password)}"}


def create_regulation(headers, **overrides) -> dict:
    payload = {
        "title": f"{TITLE_PREFIX}{uuid.uuid4().hex[:8]}",
        "due_date": "2027-06-30",
        "domain": "Batteries",
        "business_unit_ids": ["BU1"],
        "add_to_roadmap_dashboard": False,
    }
    payload.update(overrides)
    resp = httpx.post(f"{BASE_URL}/regulations", json=payload, headers=headers, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    return resp.json()


def patch(reg_id, body, headers):
    return httpx.patch(f"{BASE_URL}/regulations/{reg_id}", json=body, headers=headers, timeout=TIMEOUT)


def stored(reg_id: str) -> dict:
    snap = (
        db.collection("tenants").document(TENANT_A_ID)
        .collection("regulations").document(reg_id).get()
    )
    return snap.to_dict()


def audit_actions(reg_id: str) -> list[str]:
    docs = (
        db.collection("tenants").document(TENANT_A_ID).collection("auditLog")
        .where("targetPath", "==", f"tenants/{TENANT_A_ID}/regulations/{reg_id}")
        .stream()
    )
    return [d.to_dict()["action"] for d in docs]


def test_new_regulation_has_no_status():
    """Never defaulted: a new regulation is "not set" until a person rates it."""
    reg = create_regulation(auth_header(*TENANT_A_ADMIN))
    assert stored(reg["id"]).get("status") is None


def test_admin_sets_status_and_explanation_audited():
    headers = auth_header(*TENANT_A_ADMIN)
    reg = create_regulation(headers)

    resp = patch(reg["id"], {"status": "compliant", "status_note": "  Filed Q1 update  "}, headers)
    assert resp.status_code == 200, resp.text

    data = stored(reg["id"])
    assert data["status"] == "compliant"
    assert data["statusNote"] == "Filed Q1 update"
    assert data["statusUpdatedBy"]
    assert data["statusUpdatedAt"] >= data["createdAt"]
    assert data["domain"] == "Batteries"  # untouched fields keep their value
    assert "update_regulation" in audit_actions(reg["id"])


def test_sme_can_set_action_needed():
    reg = create_regulation(auth_header(*TENANT_A_ADMIN))
    resp = patch(reg["id"], {"status": "action_needed"}, auth_header(*TENANT_A_SME))
    assert resp.status_code == 200, resp.text
    assert stored(reg["id"])["status"] == "action_needed"


def test_invalid_status_rejected_and_nothing_written():
    headers = auth_header(*TENANT_A_ADMIN)
    reg = create_regulation(headers)
    for bad in ("green", "Compliant", "red", ""):
        resp = patch(reg["id"], {"status": bad}, headers)
        assert resp.status_code == 400, f"{bad!r} should be rejected"
        assert "status" in str(resp.json()["detail"])
    assert stored(reg["id"]).get("status") is None


def test_status_cleared_with_null_keeps_explanation():
    headers = auth_header(*TENANT_A_ADMIN)
    reg = create_regulation(headers)
    patch(reg["id"], {"status": "pending", "status_note": "Awaiting supplier data"}, headers)

    resp = patch(reg["id"], {"status": None}, headers)
    assert resp.status_code == 200, resp.text
    data = stored(reg["id"])
    assert data["status"] is None
    assert data["statusNote"] == "Awaiting supplier data"


def test_explanation_too_long_rejected():
    headers = auth_header(*TENANT_A_ADMIN)
    reg = create_regulation(headers)
    resp = patch(reg["id"], {"status": "pending", "status_note": "x" * 1001}, headers)
    assert resp.status_code == 400
    assert "status_note" in str(resp.json()["detail"])
    assert stored(reg["id"]).get("status") is None


def test_explanation_only_change_does_not_touch_status_timestamp():
    headers = auth_header(*TENANT_A_ADMIN)
    reg = create_regulation(headers)
    patch(reg["id"], {"status": "compliant"}, headers)
    first = stored(reg["id"])["statusUpdatedAt"]

    resp = patch(reg["id"], {"status_note": "Evidence on file"}, headers)
    assert resp.status_code == 200, resp.text
    data = stored(reg["id"])
    assert data["statusUpdatedAt"] == first
    assert data["lastUpdated"] > first


def test_user_cannot_set_status_yet():
    """Admin/SME only until T020 adds the BU-scoped Dashboard edit for Users."""
    reg = create_regulation(auth_header(*TENANT_A_ADMIN), business_unit_ids=["BU1"])
    resp = patch(reg["id"], {"status": "compliant"}, auth_header(*TENANT_A_USER))
    assert resp.status_code == 403
    assert stored(reg["id"]).get("status") is None


def test_user_sees_status_in_list():
    """Viewing is tenant-wide for every role (FR-1.4, FR-2.1)."""
    reg = create_regulation(auth_header(*TENANT_A_ADMIN), business_unit_ids=["BU2"])
    patch(reg["id"], {"status": "action_needed", "status_note": "Gap in labelling"}, auth_header(*TENANT_A_ADMIN))

    listed = httpx.get(f"{BASE_URL}/regulations", headers=auth_header(*TENANT_A_USER), timeout=TIMEOUT).json()
    row = next(r for r in listed if r["id"] == reg["id"])
    assert row["status"] == "action_needed"
    assert row["statusNote"] == "Gap in labelling"


def test_other_tenant_cannot_set_status():
    reg = create_regulation(auth_header(*TENANT_A_ADMIN))
    resp = patch(reg["id"], {"status": "compliant"}, auth_header(*TENANT_B_ADMIN))
    assert resp.status_code == 404
    assert stored(reg["id"]).get("status") is None
