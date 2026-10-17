"""
T022 - Internal QC for Milestone 2: edit scope on the Dashboard (FR-2.3,
FR-0.2, DL-8), and the paths no per-task test exercised.

The per-task suites (T018-T020) already prove the main rules: a User sets
only their own unit's status, Admin/SME any unit, documents per unit, other
tenants get 404. This file covers what they never did:
  - a removed regulation accepts nothing - no status, upload, hide or
    download - from any role;
  - a unit taken off a regulation can no longer be updated, and its past
    status is kept, not erased (NFR-8);
  - SME as the actual caller for status and for hiding documents;
  - unit codes match exactly ("bu1" is not "BU1");
  - the 1,000-character explanation limit at its boundary;
  - a User's upload is audited with the User's own role (NFR-2);
  - another tenant can't hide a document;
  - uploaded files: the stored and served type comes from the file's
    extension, never from what the browser claimed, and a path in the
    file name is stripped (fixes made in T022).

Titles use "T022 QC Regulation " (registered in tests/cleanup_test_data.py),
so the session cleanup removes the regulations and their files.
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

import io
import uuid
import httpx
from core.get_test_token import get_id_token
from core.tenancy import db

BASE_URL = "http://127.0.0.1:8000"
TIMEOUT = 30.0
TITLE_PREFIX = "T022 QC Regulation "

TEST_PASSWORD = os.environ["TEST_ACCOUNTS_PASSWORD"].strip()
TENANT_A_ADMIN = ("admin@testtenant.com", TEST_PASSWORD)
TENANT_A_SME = ("sme@testtenant.com", TEST_PASSWORD)
TENANT_A_USER = ("user@testtenant.com", TEST_PASSWORD)  # BU1 only
TENANT_B_ADMIN = ("admin@tenantb.com", TEST_PASSWORD)
TENANT_A_ID = "test-tenant-001"


def auth_header(email, password):
    return {"Authorization": f"Bearer {get_id_token(email, password)}"}


def create_regulation(business_unit_ids=("BU1", "BU2")) -> dict:
    resp = httpx.post(
        f"{BASE_URL}/regulations",
        json={
            "title": f"{TITLE_PREFIX}{uuid.uuid4().hex[:8]}",
            "due_date": "2027-06-30",
            "domain": "Batteries",
            "business_unit_ids": list(business_unit_ids),
            "add_to_roadmap_dashboard": True,
        },
        headers=auth_header(*TENANT_A_ADMIN),
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def set_bu(reg_id, bu, body, headers):
    return httpx.patch(f"{BASE_URL}/regulations/{reg_id}/bu-status/{bu}", json=body, headers=headers, timeout=TIMEOUT)


def upload(reg_id, bu_code, headers, name="evidence.txt", content=b"T022 evidence", ctype="text/plain"):
    return httpx.post(
        f"{BASE_URL}/regulations/{reg_id}/documents",
        data={"bu_code": bu_code},
        files={"file": (name, io.BytesIO(content), ctype)},
        headers=headers,
        timeout=TIMEOUT,
    )


def hide(reg_id, key, headers):
    return httpx.post(f"{BASE_URL}/regulations/{reg_id}/documents/hide", json={"key": key}, headers=headers, timeout=TIMEOUT)


def download(reg_id, key, headers):
    return httpx.get(f"{BASE_URL}/regulations/{reg_id}/documents/{key}", headers=headers, timeout=TIMEOUT)


def stored(reg_id: str) -> dict:
    return db.collection("tenants").document(TENANT_A_ID).collection("regulations").document(reg_id).get().to_dict()


def audit_entries(reg_id: str) -> list[dict]:
    docs = (
        db.collection("tenants").document(TENANT_A_ID).collection("auditLog")
        .where("targetPath", "==", f"tenants/{TENANT_A_ID}/regulations/{reg_id}")
        .stream()
    )
    return [d.to_dict() for d in docs]


# --- Removed regulations ------------------------------------------------------

def test_removed_regulation_accepts_nothing_from_any_role():
    reg = create_regulation()
    admin, user = auth_header(*TENANT_A_ADMIN), auth_header(*TENANT_A_USER)
    doc = upload(reg["id"], "BU1", admin).json()
    assert httpx.delete(f"{BASE_URL}/regulations/{reg['id']}", headers=admin, timeout=TIMEOUT).status_code == 200

    for headers in (admin, user):
        assert set_bu(reg["id"], "BU1", {"status": "pending"}, headers).status_code == 404
        assert upload(reg["id"], "BU1", headers).status_code == 404
        assert download(reg["id"], doc["key"], headers).status_code == 404
    assert hide(reg["id"], doc["key"], admin).status_code == 404
    assert not (stored(reg["id"]).get("buStatuses") or {})


# --- Units on a regulation ----------------------------------------------------

def test_unit_taken_off_a_regulation_keeps_its_history_but_cannot_be_updated():
    reg = create_regulation()
    admin = auth_header(*TENANT_A_ADMIN)
    assert set_bu(reg["id"], "BU2", {"status": "action_needed", "note": "Gap"}, admin).status_code == 200

    resp = httpx.patch(f"{BASE_URL}/regulations/{reg['id']}", json={"business_unit_ids": ["BU1"]}, headers=admin, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text

    assert set_bu(reg["id"], "BU2", {"status": "compliant"}, admin).status_code == 400
    kept = stored(reg["id"])["buStatuses"]["BU2"]
    assert kept["status"] == "action_needed" and kept["note"] == "Gap"  # kept, not erased (NFR-8)


def test_unit_codes_match_exactly():
    reg = create_regulation()
    resp = set_bu(reg["id"], "bu1", {"status": "pending"}, auth_header(*TENANT_A_USER))
    assert resp.status_code == 403
    assert "not permitted" in resp.json()["detail"]


# --- SME as the caller ----------------------------------------------------------

def test_sme_sets_any_unit_and_hides_documents():
    reg = create_regulation()
    sme = auth_header(*TENANT_A_SME)
    assert set_bu(reg["id"], "BU2", {"status": "compliant"}, sme).status_code == 200
    doc = upload(reg["id"], "BU2", sme).json()
    assert hide(reg["id"], doc["key"], sme).status_code == 200
    actions = [(e["action"], e["actorRole"]) for e in audit_entries(reg["id"])]
    assert ("update_bu_status", "sme") in actions and ("hide_regulation_document", "sme") in actions


# --- Explanation limit -------------------------------------------------------------

def test_explanation_limit_is_exactly_1000_characters():
    reg = create_regulation()
    user = auth_header(*TENANT_A_USER)
    assert set_bu(reg["id"], "BU1", {"note": "x" * 1000}, user).status_code == 200
    assert set_bu(reg["id"], "BU1", {"note": "x" * 1001}, user).status_code == 400
    assert len(stored(reg["id"])["buStatuses"]["BU1"]["note"]) == 1000


# --- Audit ---------------------------------------------------------------------------

def test_user_upload_is_audited_as_the_user():
    reg = create_regulation()
    user = auth_header(*TENANT_A_USER)
    my_uid = httpx.get(f"{BASE_URL}/me", headers=user, timeout=TIMEOUT).json()["uid"]
    assert upload(reg["id"], "BU1", user).status_code == 200
    entries = [e for e in audit_entries(reg["id"]) if e["action"] == "add_regulation_document"]
    assert len(entries) == 1
    assert entries[0]["actorUid"] == my_uid and entries[0]["actorRole"] == "user"


# --- Tenants ---------------------------------------------------------------------------

def test_other_tenant_cannot_hide_a_document():
    reg = create_regulation()
    doc = upload(reg["id"], "BU1", auth_header(*TENANT_A_ADMIN)).json()
    assert hide(reg["id"], doc["key"], auth_header(*TENANT_B_ADMIN)).status_code == 404
    assert not (stored(reg["id"]).get("hiddenDocuments") or {})


# --- Uploaded files (T022 fixes) ----------------------------------------------------

def test_file_type_comes_from_the_extension_not_the_browser():
    reg = create_regulation()
    admin = auth_header(*TENANT_A_ADMIN)
    resp = upload(reg["id"], "BU1", admin, name="notes.txt", content=b"<script>alert(1)</script>", ctype="text/html")
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    assert doc["contentType"] == "text/plain"

    got = download(reg["id"], doc["key"], admin)
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("text/plain")
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["content-disposition"].startswith("attachment")


def test_path_in_file_name_is_stripped():
    reg = create_regulation()
    admin = auth_header(*TENANT_A_ADMIN)
    resp = upload(reg["id"], "BU1", admin, name="../../other-tenant/evidence.txt")
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    assert doc["fileName"] == "evidence.txt"
    assert doc["gcsUri"].startswith(f"gs://regulator-dev-documents/tenants/{TENANT_A_ID}/regulation-documents/")
    assert ".." not in doc["gcsUri"]
    assert download(reg["id"], doc["key"], admin).status_code == 200
