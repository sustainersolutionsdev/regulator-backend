"""
Testing feedback, Oct 26 (Srinivas, Oct 24 US discussion + feedback document):

  - Item 6: documents can be added from the Roadmap Edit panel. Without a
    bu_code, POST /regulations/{id}/documents adds a regulation-wide
    document (like Add Regulation's): Admin/SME only. Users still add
    documents for their own Business Unit(s) on the Dashboard.
  - Item 7 / feedback 2: PATCH /business-units/{code} renames a unit
    (label only; the code stays). Admin/SME only, own tenant only, audited.
  - T058: GET /health reports the release version and commit.

Regulations use the registered "T020 Document Regulation " prefix and units
the registered "BU-T006-TEST-" prefix, so the session cleanup removes them.
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

import io
import pathlib
import uuid
import httpx
from core.get_test_token import get_id_token
from core.tenancy import db

BASE_URL = "http://127.0.0.1:8000"
TIMEOUT = 30.0
TITLE_PREFIX = "T020 Document Regulation "

TEST_PASSWORD = os.environ["TEST_ACCOUNTS_PASSWORD"].strip()
TENANT_A_ADMIN = ("admin@testtenant.com", TEST_PASSWORD)
TENANT_A_SME = ("sme@testtenant.com", TEST_PASSWORD)
TENANT_A_USER = ("user@testtenant.com", TEST_PASSWORD)  # BU1 only
TENANT_B_ADMIN = ("admin@tenantb.com", TEST_PASSWORD)
TENANT_A_ID = "test-tenant-001"


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


def upload(reg_id, headers, bu_code=None, name="roadmap-edit.txt"):
    data = {"bu_code": bu_code} if bu_code is not None else {}
    return httpx.post(
        f"{BASE_URL}/regulations/{reg_id}/documents",
        data=data,
        files={"file": (name, io.BytesIO(b"Roadmap edit upload"), "text/plain")},
        headers=headers,
        timeout=TIMEOUT,
    )


def stored_docs(reg_id):
    return (db.collection("tenants").document(TENANT_A_ID).collection("regulations").document(reg_id)
            .get().to_dict() or {}).get("documents", [])


# --- Item 6: regulation-wide documents --------------------------------------

def test_admin_adds_regulation_wide_document_without_unit():
    reg = create_regulation()
    resp = upload(reg["id"], auth_header(*TENANT_A_ADMIN))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "buCode" not in body and body["key"] and body["hidden"] is False
    docs = stored_docs(reg["id"])
    assert len(docs) == 1 and "buCode" not in docs[0]


def test_sme_adds_regulation_wide_document():
    reg = create_regulation()
    assert upload(reg["id"], auth_header(*TENANT_A_SME)).status_code == 200


def test_user_cannot_add_regulation_wide_document():
    reg = create_regulation()
    resp = upload(reg["id"], auth_header(*TENANT_A_USER))
    assert resp.status_code == 403
    assert stored_docs(reg["id"]) == []


def test_blank_unit_counts_as_regulation_wide():
    reg = create_regulation()
    assert upload(reg["id"], auth_header(*TENANT_A_USER), bu_code="  ").status_code == 403
    assert upload(reg["id"], auth_header(*TENANT_A_ADMIN), bu_code="").status_code == 200


def test_user_still_adds_for_own_unit():
    reg = create_regulation()
    resp = upload(reg["id"], auth_header(*TENANT_A_USER), bu_code="BU1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["buCode"] == "BU1"


def test_other_tenant_cannot_add_regulation_wide_document():
    reg = create_regulation()
    assert upload(reg["id"], auth_header(*TENANT_B_ADMIN)).status_code == 404


# --- Item 7: rename a Business Unit -----------------------------------------

def make_unit(label="Original name") -> str:
    code = f"BU-T006-TEST-{uuid.uuid4().hex[:6]}"
    resp = httpx.post(f"{BASE_URL}/business-units", params={"bu_code": code, "label": label},
                      headers=auth_header(*TENANT_A_ADMIN), timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    return code


def rename(code, label, headers):
    return httpx.patch(f"{BASE_URL}/business-units/{code}", json={"label": label}, headers=headers, timeout=TIMEOUT)


def listed_label(code, headers):
    units = httpx.get(f"{BASE_URL}/business-units", headers=headers, timeout=TIMEOUT).json()
    return next((u["label"] for u in units if u["code"] == code), None)


def test_rename_requires_token():
    assert httpx.patch(f"{BASE_URL}/business-units/BU1", json={"label": "x"}, timeout=TIMEOUT).status_code == 401


def test_admin_renames_unit_and_code_stays():
    code = make_unit()
    resp = rename(code, "  Printing   Systems ", auth_header(*TENANT_A_ADMIN))
    assert resp.status_code == 200, resp.text
    assert resp.json()["label"] == "Printing Systems" and resp.json()["code"] == code
    assert listed_label(code, auth_header(*TENANT_A_USER)) == "Printing Systems"


def test_rename_is_audited():
    code = make_unit()
    assert rename(code, "Audited name", auth_header(*TENANT_A_SME)).status_code == 200
    entries = (db.collection("tenants").document(TENANT_A_ID).collection("auditLog")
               .where("action", "==", "rename_business_unit").stream())
    paths = [e.to_dict() for e in entries if e.to_dict().get("targetPath", "").endswith(f"/businessUnits/{code}")]
    assert paths and paths[-1]["after"] == {"label": "Audited name"} and paths[-1]["actorRole"] == "sme"


def test_user_cannot_rename():
    code = make_unit()
    assert rename(code, "User attempt", auth_header(*TENANT_A_USER)).status_code == 403
    assert listed_label(code, auth_header(*TENANT_A_ADMIN)) == "Original name"


def test_blank_and_long_names_refused():
    code = make_unit()
    admin = auth_header(*TENANT_A_ADMIN)
    assert rename(code, "   ", admin).status_code == 400
    assert rename(code, "x" * 81, admin).status_code == 400
    assert rename(code, "x" * 80, admin).status_code == 200


def test_unknown_or_other_tenant_unit_is_404():
    code = make_unit()
    assert rename("BU-DOES-NOT-EXIST", "x", auth_header(*TENANT_A_ADMIN)).status_code == 404
    assert rename(code, "Cross-tenant", auth_header(*TENANT_B_ADMIN)).status_code == 404
    assert listed_label(code, auth_header(*TENANT_A_ADMIN)) == "Original name"


# --- T058: version on /health -----------------------------------------------

def test_health_reports_version():
    body = httpx.get(f"{BASE_URL}/health", timeout=TIMEOUT).json()
    version = (pathlib.Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
    assert body["status"] == "ok" and body["version"] == version and body["commit"]
