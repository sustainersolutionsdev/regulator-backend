import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

import io
import uuid
import httpx
from core.get_test_token import get_id_token
from core.tenancy import db  # direct Firestore read-back — no GET /regulations exists yet

BASE_URL = "http://127.0.0.1:8000"
TIMEOUT = 30.0

TEST_PASSWORD = os.environ["TEST_ACCOUNTS_PASSWORD"].strip()

TENANT_A_ADMIN = ("admin@testtenant.com", TEST_PASSWORD)
TENANT_A_USER = ("user@testtenant.com", TEST_PASSWORD)


def auth_header(email, password):
    token = get_id_token(email, password)
    return {"Authorization": f"Bearer {token}"}


def minimal_valid_payload(title_suffix: str = None) -> dict:
    suffix = title_suffix or uuid.uuid4().hex[:8]
    return {
        "title": f"Test Regulation {suffix}",
        "abbreviated_title": "Test Reg",
        "due_date": "2027-01-01",
        "domain": "Chemicals",
        "country": "US",
        "region": "Federal",
        "business_unit_ids": ["BU1"],
        "notes": "Smoke test entry",
        "add_to_roadmap_dashboard": True,
    }


def test_user_role_cannot_create_regulation():
    headers = auth_header(*TENANT_A_USER)
    resp = httpx.post(
        f"{BASE_URL}/regulations",
        json=minimal_valid_payload(),
        headers=headers,
        timeout=TIMEOUT,
    )
    assert resp.status_code == 403
    assert "Admin or SME" in resp.json()["detail"]


def test_admin_can_create_regulation_with_corrected_fields():
    headers = auth_header(*TENANT_A_ADMIN)
    payload = minimal_valid_payload()

    resp = httpx.post(f"{BASE_URL}/regulations", json=payload, headers=headers, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["title"] == payload["title"]
    assert body["domain"] == "Chemicals"
    assert body["businessUnitIds"] == ["BU1"]
    assert body["addToRoadmapDashboard"] is True
    assert body["hidden"] is False
    assert "id" in body and body["id"]
    assert "createdAt" in body
    assert "lastUpdated" in body


def test_minimal_payload_with_only_required_fields_succeeds():
    """abbreviated_title, country, region, notes, documents are all
    optional per the corrected field set — confirm the endpoint doesn't
    silently require any of them."""
    headers = auth_header(*TENANT_A_ADMIN)
    payload = {
        "title": f"Required-Only Test {uuid.uuid4().hex[:8]}",
        "due_date": "2027-01-01",
        "domain": "Semiconductors",
        "business_unit_ids": ["BU1"],
    }
    resp = httpx.post(f"{BASE_URL}/regulations", json=payload, headers=headers, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text


def test_missing_required_field_returns_422():
    """title is required and non-Optional on AddRegulationRequest, so
    Pydantic rejects this before validate_regulation_payload() runs."""
    headers = auth_header(*TENANT_A_ADMIN)
    payload = minimal_valid_payload()
    del payload["title"]

    resp = httpx.post(f"{BASE_URL}/regulations", json=payload, headers=headers, timeout=TIMEOUT)
    assert resp.status_code == 422


def test_empty_business_unit_list_returns_400():
    """business_unit_ids=[] passes Pydantic (a valid, present list) but
    fails validate_regulation_payload()'s emptiness check."""
    headers = auth_header(*TENANT_A_ADMIN)
    payload = minimal_valid_payload()
    payload["business_unit_ids"] = []

    resp = httpx.post(f"{BASE_URL}/regulations", json=payload, headers=headers, timeout=TIMEOUT)
    assert resp.status_code == 400
    assert "business_unit_ids" in str(resp.json()["detail"])


def test_id_and_timestamps_are_server_generated_not_client_supplied():
    headers = auth_header(*TENANT_A_ADMIN)
    payload = minimal_valid_payload()
    payload["id"] = "client-supplied-should-be-dropped"

    resp = httpx.post(f"{BASE_URL}/regulations", json=payload, headers=headers, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] != "client-supplied-should-be-dropped"


def test_regulation_write_produces_audit_log_entry():
    headers = auth_header(*TENANT_A_ADMIN)
    payload = minimal_valid_payload()

    resp = httpx.post(f"{BASE_URL}/regulations", json=payload, headers=headers, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    reg_id = resp.json()["id"]

    audit_docs = list(
        db.collection("tenants").document("test-tenant-001")
        .collection("auditLog")
        .where("action", "==", "create_regulation")
        .where("targetPath", "==", f"tenants/test-tenant-001/regulations/{reg_id}")
        .stream()
    )
    assert len(audit_docs) == 1
    assert audit_docs[0].to_dict()["actorRole"] == "admin"


def test_user_role_cannot_upload_document():
    headers = auth_header(*TENANT_A_USER)
    files = {"file": ("test.txt", io.BytesIO(b"hello"), "text/plain")}
    resp = httpx.post(f"{BASE_URL}/regulations/upload-document", headers=headers, files=files, timeout=TIMEOUT)
    assert resp.status_code == 403


def test_admin_can_upload_document_and_reference_it_on_create():
    headers = auth_header(*TENANT_A_ADMIN)
    files = {"file": ("test.txt", io.BytesIO(b"hello world"), "text/plain")}

    upload_resp = httpx.post(
        f"{BASE_URL}/regulations/upload-document", headers=headers, files=files, timeout=TIMEOUT
    )
    assert upload_resp.status_code == 200, upload_resp.text
    doc_meta = upload_resp.json()
    assert doc_meta["gcs_uri"].startswith("gs://regulator-dev-documents/tenants/test-tenant-001/")
    assert doc_meta["size_bytes"] == len(b"hello world")

    payload = minimal_valid_payload()
    payload["documents"] = [doc_meta]

    create_resp = httpx.post(f"{BASE_URL}/regulations", json=payload, headers=headers, timeout=TIMEOUT)
    assert create_resp.status_code == 200, create_resp.text
    assert create_resp.json()["documents"][0]["gcsUri"] == doc_meta["gcs_uri"]


def test_disallowed_file_type_returns_400():
    headers = auth_header(*TENANT_A_ADMIN)
    files = {"file": ("virus.exe", io.BytesIO(b"binary"), "application/octet-stream")}
    resp = httpx.post(f"{BASE_URL}/regulations/upload-document", headers=headers, files=files, timeout=TIMEOUT)
    assert resp.status_code == 400
    assert "not allowed" in resp.json()["detail"]


def test_powerpoint_file_type_is_allowed():
    """Confirmed by Srinivas (9/17): PowerPoint added alongside PDF/Word/
    Excel/text as an allowed upload type."""
    headers = auth_header(*TENANT_A_ADMIN)
    files = {"file": ("slides.pptx", io.BytesIO(b"fake pptx bytes"), "application/vnd.openxmlformats-officedocument.presentationml.presentation")}
    resp = httpx.post(f"{BASE_URL}/regulations/upload-document", headers=headers, files=files, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    assert resp.json()["file_name"] == "slides.pptx"
