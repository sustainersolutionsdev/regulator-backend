"""
T014 - Edit / remove a regulation: PATCH and DELETE /regulations/{id}.

Rules (Srinivas, Oct 13 2026): Admin and SME edit and remove; Users do
neither from the Roadmap. Removal is a soft delete (NFR-8), audited (NFR-2),
and never crosses tenants (NFR-1).

Every regulation created here is titled "T014 Edit Regulation ..." so
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
TITLE_PREFIX = "T014 Edit Regulation "

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
        "due_date": "2027-03-31",
        "domain": "Batteries",
        "business_unit_ids": ["BU1"],
        "add_to_roadmap_dashboard": False,
    }
    payload.update(overrides)
    resp = httpx.post(f"{BASE_URL}/regulations", json=payload, headers=headers, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    return resp.json()


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


# --- Authentication / role gate -------------------------------------------

def test_edit_and_remove_reject_missing_token():
    assert httpx.patch(f"{BASE_URL}/regulations/x", json={"title": "a"}, timeout=TIMEOUT).status_code == 401
    assert httpx.delete(f"{BASE_URL}/regulations/x", timeout=TIMEOUT).status_code == 401


def test_user_cannot_edit_even_in_own_business_unit():
    reg = create_regulation(auth_header(*TENANT_A_ADMIN), business_unit_ids=["BU1"])
    resp = httpx.patch(
        f"{BASE_URL}/regulations/{reg['id']}",
        json={"notes": "user attempt"},
        headers=auth_header(*TENANT_A_USER),
        timeout=TIMEOUT,
    )
    assert resp.status_code == 403
    assert "Admin or SME" in resp.json()["detail"]
    assert stored(reg["id"]).get("notes", "") == ""


def test_user_cannot_remove():
    reg = create_regulation(auth_header(*TENANT_A_ADMIN))
    resp = httpx.delete(
        f"{BASE_URL}/regulations/{reg['id']}", headers=auth_header(*TENANT_A_USER), timeout=TIMEOUT
    )
    assert resp.status_code == 403
    assert stored(reg["id"])["hidden"] is False


# --- Editing ----------------------------------------------------------------

def test_admin_edits_fields_and_change_is_audited():
    headers = auth_header(*TENANT_A_ADMIN)
    reg = create_regulation(headers)
    new_title = f"{TITLE_PREFIX}renamed {uuid.uuid4().hex[:6]}"

    resp = httpx.patch(
        f"{BASE_URL}/regulations/{reg['id']}",
        json={"title": new_title, "due_date": "2028-06-30", "region": "California", "notes": "Updated scope"},
        headers=headers,
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, resp.text

    data = stored(reg["id"])
    assert data["title"] == new_title
    assert data["dueDate"] == "2028-06-30"
    assert data["region"] == "California"
    assert data["notes"] == "Updated scope"
    assert data["domain"] == "Batteries"  # untouched fields keep their value
    assert data["lastUpdated"] > data["createdAt"]
    assert "update_regulation" in audit_actions(reg["id"])


def test_sme_can_edit():
    headers = auth_header(*TENANT_A_SME)
    reg = create_regulation(auth_header(*TENANT_A_ADMIN))
    resp = httpx.patch(
        f"{BASE_URL}/regulations/{reg['id']}",
        json={"business_unit_ids": ["BU1", "BU2"]},
        headers=headers,
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, resp.text
    assert stored(reg["id"])["businessUnitIds"] == ["BU1", "BU2"]


def test_required_fields_cannot_be_blanked():
    headers = auth_header(*TENANT_A_ADMIN)
    reg = create_regulation(headers)
    resp = httpx.patch(
        f"{BASE_URL}/regulations/{reg['id']}",
        json={"title": "  ", "business_unit_ids": []},
        headers=headers,
        timeout=TIMEOUT,
    )
    assert resp.status_code == 400
    detail = str(resp.json()["detail"])
    assert "title" in detail and "business_unit_ids" in detail


def test_bad_due_date_rejected():
    headers = auth_header(*TENANT_A_ADMIN)
    reg = create_regulation(headers)
    resp = httpx.patch(
        f"{BASE_URL}/regulations/{reg['id']}", json={"due_date": "31/12/2027"}, headers=headers, timeout=TIMEOUT
    )
    assert resp.status_code == 400


def test_empty_patch_rejected():
    headers = auth_header(*TENANT_A_ADMIN)
    reg = create_regulation(headers)
    resp = httpx.patch(f"{BASE_URL}/regulations/{reg['id']}", json={}, headers=headers, timeout=TIMEOUT)
    assert resp.status_code == 400


def test_unknown_regulation_returns_404():
    resp = httpx.patch(
        f"{BASE_URL}/regulations/does-not-exist",
        json={"notes": "x"},
        headers=auth_header(*TENANT_A_ADMIN),
        timeout=TIMEOUT,
    )
    assert resp.status_code == 404


# --- Tenant isolation (NFR-1) ----------------------------------------------

def test_other_tenant_cannot_edit_or_remove():
    reg = create_regulation(auth_header(*TENANT_A_ADMIN))
    b_headers = auth_header(*TENANT_B_ADMIN)

    edit = httpx.patch(
        f"{BASE_URL}/regulations/{reg['id']}", json={"notes": "cross-tenant"}, headers=b_headers, timeout=TIMEOUT
    )
    remove = httpx.delete(f"{BASE_URL}/regulations/{reg['id']}", headers=b_headers, timeout=TIMEOUT)

    assert edit.status_code == 404, "Tenant B must not be able to see Tenant A's regulation"
    assert remove.status_code == 404
    data = stored(reg["id"])
    assert data.get("notes", "") == ""
    assert data["hidden"] is False


# --- Removal (soft delete, NFR-8) --------------------------------------------

def test_remove_hides_regulation_but_keeps_record():
    headers = auth_header(*TENANT_A_ADMIN)
    reg = create_regulation(headers, add_to_roadmap_dashboard=True)

    resp = httpx.delete(f"{BASE_URL}/regulations/{reg['id']}", headers=headers, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text

    listed = httpx.get(f"{BASE_URL}/regulations", headers=headers, timeout=TIMEOUT).json()
    assert reg["id"] not in {r["id"] for r in listed}

    data = stored(reg["id"])  # still in Firestore: soft delete only
    assert data["hidden"] is True
    assert data["hiddenBy"]
    assert "hide_regulation" in audit_actions(reg["id"])


def test_removed_regulation_cannot_be_edited_or_removed_again():
    headers = auth_header(*TENANT_A_ADMIN)
    reg = create_regulation(headers)
    httpx.delete(f"{BASE_URL}/regulations/{reg['id']}", headers=headers, timeout=TIMEOUT)

    again = httpx.delete(f"{BASE_URL}/regulations/{reg['id']}", headers=headers, timeout=TIMEOUT)
    edit = httpx.patch(
        f"{BASE_URL}/regulations/{reg['id']}", json={"notes": "x"}, headers=headers, timeout=TIMEOUT
    )
    assert again.status_code == 404
    assert edit.status_code == 404
