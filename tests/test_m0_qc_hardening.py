"""
T009 - Internal QC: server-side RBAC & tenant-isolation gaps (NFR-1, NFR-2).

Covers what the per-task suites (T005-T008) never exercised:
  - every M0 endpoint rejects a request with no token
  - a bad token gets a generic 401 with no exception detail
  - the SME role, as an actual caller
  - User role can see its own tenant's directory, never another tenant's
    (Srinivas, Oct 8 2026: all roles see the directory, tenant-scoped)
  - client-supplied document metadata on POST /regulations
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

import io
import uuid
import httpx
import pytest
from core.get_test_token import get_id_token

BASE_URL = "http://127.0.0.1:8000"
TIMEOUT = 30.0

TEST_PASSWORD = os.environ["TEST_ACCOUNTS_PASSWORD"].strip()

TENANT_A_ADMIN = ("admin@testtenant.com", TEST_PASSWORD)
TENANT_A_SME = ("sme@testtenant.com", TEST_PASSWORD)
TENANT_A_USER = ("user@testtenant.com", TEST_PASSWORD)
TENANT_B_ADMIN = ("admin@tenantb.com", TEST_PASSWORD)

TENANT_A_ID = "test-tenant-001"
BUCKET = "regulator-dev-documents"


def auth_header(email, password):
    token = get_id_token(email, password)
    return {"Authorization": f"Bearer {token}"}


def regulation_payload(**overrides) -> dict:
    payload = {
        "title": f"T009 QC Regulation {uuid.uuid4().hex[:8]}",
        "due_date": "2027-01-01",
        "domain": "Chemicals",
        "business_unit_ids": ["BU1"],
        "add_to_roadmap_dashboard": False,
    }
    payload.update(overrides)
    return payload


def fake_document(gcs_uri: str, uploaded_by: str = "someone-else") -> dict:
    return {
        "file_name": "evidence.pdf",
        "gcs_uri": gcs_uri,
        "content_type": "application/pdf",
        "size_bytes": 10,
        "uploaded_by": uploaded_by,
        "uploaded_at": "2026-10-08T00:00:00+00:00",
    }


# --- Authentication: every M0 endpoint, no exceptions ---------------------

M0_ENDPOINTS = [
    ("GET", "/me"),
    ("GET", "/users"),
    ("POST", "/users"),
    ("GET", "/business-units"),
    ("POST", "/business-units"),
    ("POST", "/regulations"),
    ("POST", "/regulations/upload-document"),
]


@pytest.mark.parametrize("method,path", M0_ENDPOINTS)
def test_every_m0_endpoint_rejects_missing_token(method, path):
    resp = httpx.request(method, f"{BASE_URL}{path}", timeout=TIMEOUT)
    assert resp.status_code == 401, f"{method} {path} returned {resp.status_code}"


def test_invalid_token_returns_generic_401_without_detail():
    resp = httpx.get(
        f"{BASE_URL}/me",
        headers={"Authorization": "Bearer not-a-real-token"},
        timeout=TIMEOUT,
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid or expired token."
    assert "DEBUG" not in resp.text
    assert "Error" not in resp.text


# --- SME role as a real caller ----------------------------------------------

def test_sme_token_carries_sme_role_and_all_bus():
    headers = auth_header(*TENANT_A_SME)
    me = httpx.get(f"{BASE_URL}/me", headers=headers, timeout=TIMEOUT)
    assert me.status_code == 200, me.text
    assert me.json()["role"] == "sme"
    assert me.json()["tenant_id"] == TENANT_A_ID
    assert "BU1" in me.json()["businessUnitIds"]


def test_sme_can_create_regulation():
    headers = auth_header(*TENANT_A_SME)
    resp = httpx.post(
        f"{BASE_URL}/regulations", json=regulation_payload(), headers=headers, timeout=TIMEOUT
    )
    assert resp.status_code == 200, resp.text


def test_sme_bypasses_bu_write_restriction():
    headers = auth_header(*TENANT_A_SME)
    resp = httpx.post(f"{BASE_URL}/test-write/BU2", headers=headers, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text


# --- People Directory: all roles, own tenant only ---------------------------

def test_user_role_sees_own_tenant_directory_only():
    headers = auth_header(*TENANT_A_USER)
    resp = httpx.get(f"{BASE_URL}/users", headers=headers, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    emails = {u.get("email", "") for u in resp.json()}
    assert "user@testtenant.com" in emails
    assert not any(e.endswith("@tenantb.com") for e in emails), \
        "LEAK: Tenant A user saw a Tenant B directory entry"


# --- Document metadata on POST /regulations ---------------------------------

def test_cannot_attach_another_tenants_document():
    """Tenant B admin points a regulation at a file in Tenant A's folder."""
    headers = auth_header(*TENANT_B_ADMIN)
    tenant_a_uri = f"gs://{BUCKET}/tenants/{TENANT_A_ID}/regulation-documents/x-evidence.pdf"
    resp = httpx.post(
        f"{BASE_URL}/regulations",
        json=regulation_payload(documents=[fake_document(tenant_a_uri)]),
        headers=headers,
        timeout=TIMEOUT,
    )
    assert resp.status_code == 400, resp.text
    assert "does not belong to this tenant" in resp.json()["detail"]


def test_path_traversal_in_document_uri_rejected():
    headers = auth_header(*TENANT_A_ADMIN)
    sneaky = f"gs://{BUCKET}/tenants/{TENANT_A_ID}/../tenant-b/secret.pdf"
    resp = httpx.post(
        f"{BASE_URL}/regulations",
        json=regulation_payload(documents=[fake_document(sneaky)]),
        headers=headers,
        timeout=TIMEOUT,
    )
    assert resp.status_code == 400, resp.text


def test_uploaded_by_is_set_by_server_not_client():
    headers = auth_header(*TENANT_A_ADMIN)
    my_uid = httpx.get(f"{BASE_URL}/me", headers=headers, timeout=TIMEOUT).json()["uid"]

    files = {"file": ("t009.txt", io.BytesIO(b"t009 evidence"), "text/plain")}
    upload = httpx.post(
        f"{BASE_URL}/regulations/upload-document", headers=headers, files=files, timeout=TIMEOUT
    )
    assert upload.status_code == 200, upload.text
    doc_meta = upload.json()
    doc_meta["uploaded_by"] = "spoofed-uid"

    resp = httpx.post(
        f"{BASE_URL}/regulations",
        json=regulation_payload(documents=[doc_meta]),
        headers=headers,
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["documents"][0]["uploadedBy"] == my_uid


def test_null_documents_does_not_crash():
    headers = auth_header(*TENANT_A_ADMIN)
    resp = httpx.post(
        f"{BASE_URL}/regulations",
        json=regulation_payload(documents=None),
        headers=headers,
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["documents"] == []
