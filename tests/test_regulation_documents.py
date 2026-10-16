"""
T020 - Supporting documents on the Dashboard (FR-2.3, DL-8).

  - POST /regulations/{id}/documents: Admin/SME any unit; User only their
    own unit(s). Each document is tagged with the unit and the uploader.
  - POST /regulations/{id}/documents/hide: Admin/SME only; soft (NFR-8).
  - GET /regulations/{id}/documents/{key}: any role in the tenant; hidden
    documents and other tenants' regulations return 404 (NFR-1).

Titles use "T020 Document Regulation " (registered in
tests/cleanup_test_data.py), so the regulations AND their uploaded files
are removed after the session.
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
TITLE_PREFIX = "T020 Document Regulation "

TEST_PASSWORD = os.environ["TEST_ACCOUNTS_PASSWORD"].strip()
TENANT_A_ADMIN = ("admin@testtenant.com", TEST_PASSWORD)
TENANT_A_USER = ("user@testtenant.com", TEST_PASSWORD)  # BU1 only
TENANT_B_ADMIN = ("admin@tenantb.com", TEST_PASSWORD)
TENANT_A_ID = "test-tenant-001"

FILE_BYTES = b"T020 evidence file"


def auth_header(email, password):
    return {"Authorization": f"Bearer {get_id_token(email, password)}"}


def create_regulation() -> dict:
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
    return resp.json()


def upload(reg_id, bu_code, headers, name="evidence.txt", content=FILE_BYTES, ctype="text/plain"):
    return httpx.post(
        f"{BASE_URL}/regulations/{reg_id}/documents",
        data={"bu_code": bu_code},
        files={"file": (name, io.BytesIO(content), ctype)},
        headers=headers,
        timeout=TIMEOUT,
    )


def listed(reg_id, headers) -> dict:
    rows = httpx.get(f"{BASE_URL}/regulations", headers=headers, timeout=TIMEOUT).json()
    return next(r for r in rows if r["id"] == reg_id)


def test_document_endpoints_require_token():
    assert httpx.post(f"{BASE_URL}/regulations/x/documents", timeout=TIMEOUT).status_code == 401
    assert httpx.post(f"{BASE_URL}/regulations/x/documents/hide", json={"key": "k"}, timeout=TIMEOUT).status_code == 401
    assert httpx.get(f"{BASE_URL}/regulations/x/documents/k", timeout=TIMEOUT).status_code == 401


def test_user_uploads_for_own_unit_and_it_is_tagged():
    reg = create_regulation()
    user = auth_header(*TENANT_A_USER)
    my_uid = httpx.get(f"{BASE_URL}/me", headers=user, timeout=TIMEOUT).json()["uid"]

    resp = upload(reg["id"], "BU1", user)
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    assert doc["buCode"] == "BU1"
    assert doc["uploadedBy"] == my_uid
    assert doc["gcsUri"].startswith(f"gs://regulator-dev-documents/tenants/{TENANT_A_ID}/")
    assert doc["key"] and doc["hidden"] is False

    row = listed(reg["id"], user)
    assert any(d["key"] == doc["key"] and d["hidden"] is False for d in row["documents"])


def test_user_cannot_upload_for_another_unit():
    reg = create_regulation()
    resp = upload(reg["id"], "BU2", auth_header(*TENANT_A_USER))
    assert resp.status_code == 403
    assert "not permitted" in resp.json()["detail"]


def test_disallowed_file_type_rejected():
    reg = create_regulation()
    resp = upload(reg["id"], "BU1", auth_header(*TENANT_A_ADMIN), name="tool.exe", ctype="application/octet-stream")
    assert resp.status_code == 400
    assert "not allowed" in resp.json()["detail"]


def test_any_role_downloads_the_file():
    reg = create_regulation()
    doc = upload(reg["id"], "BU2", auth_header(*TENANT_A_ADMIN)).json()
    resp = httpx.get(
        f"{BASE_URL}/regulations/{reg['id']}/documents/{doc['key']}",
        headers=auth_header(*TENANT_A_USER), timeout=TIMEOUT,
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == FILE_BYTES
    assert "evidence.txt" in resp.headers["content-disposition"]


def test_user_cannot_hide_a_document():
    reg = create_regulation()
    doc = upload(reg["id"], "BU1", auth_header(*TENANT_A_USER)).json()
    resp = httpx.post(
        f"{BASE_URL}/regulations/{reg['id']}/documents/hide", json={"key": doc["key"]},
        headers=auth_header(*TENANT_A_USER), timeout=TIMEOUT,
    )
    assert resp.status_code == 403


def test_admin_hides_document_softly():
    reg = create_regulation()
    admin = auth_header(*TENANT_A_ADMIN)
    doc = upload(reg["id"], "BU1", admin).json()

    resp = httpx.post(f"{BASE_URL}/regulations/{reg['id']}/documents/hide", json={"key": doc["key"]}, headers=admin, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text

    row = listed(reg["id"], admin)
    assert next(d for d in row["documents"] if d["key"] == doc["key"])["hidden"] is True
    # Soft: the entry is still in the stored record.
    data = db.collection("tenants").document(TENANT_A_ID).collection("regulations").document(reg["id"]).get().to_dict()
    assert any(d["gcsUri"] == doc["gcsUri"] for d in data["documents"])
    # Hidden documents can't be downloaded or hidden again.
    assert httpx.get(f"{BASE_URL}/regulations/{reg['id']}/documents/{doc['key']}", headers=admin, timeout=TIMEOUT).status_code == 404
    assert httpx.post(f"{BASE_URL}/regulations/{reg['id']}/documents/hide", json={"key": doc["key"]}, headers=admin, timeout=TIMEOUT).status_code == 404


def test_other_tenant_cannot_download_or_upload():
    reg = create_regulation()
    doc = upload(reg["id"], "BU1", auth_header(*TENANT_A_ADMIN)).json()
    b_admin = auth_header(*TENANT_B_ADMIN)
    assert httpx.get(f"{BASE_URL}/regulations/{reg['id']}/documents/{doc['key']}", headers=b_admin, timeout=TIMEOUT).status_code == 404
    assert upload(reg["id"], "BU1", b_admin).status_code == 404


def test_unknown_document_key_returns_404():
    reg = create_regulation()
    resp = httpx.get(f"{BASE_URL}/regulations/{reg['id']}/documents/0000000000000000",
                     headers=auth_header(*TENANT_A_ADMIN), timeout=TIMEOUT)
    assert resp.status_code == 404
