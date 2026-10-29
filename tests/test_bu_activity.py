"""
T065 - Business Unit activity, against the server (Srinivas, Oct 29).

  - PATCH /regulations/{id}/bu-status/{bu} records one activity entry per
    save: status change, note and the documents attached with it.
  - GET /regulations/{id}/activity: every role, newest first, with names.
  - DELETE /regulations/{id}/activity/{entry}: your own note only, soft;
    never a status change or a document.
  - /me carries the person's name (T066).

Titles use "T018 Status Regulation " (registered in tests/cleanup_test_data.py);
the cleanup also removes their activity entries.
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
TITLE_PREFIX = "T018 Status Regulation "

TEST_PASSWORD = os.environ["TEST_ACCOUNTS_PASSWORD"].strip()
TENANT_A_ADMIN = ("admin@testtenant.com", TEST_PASSWORD)
TENANT_A_USER = ("user@testtenant.com", TEST_PASSWORD)  # BU1 only
TENANT_B_ADMIN = ("admin@tenantb.com", TEST_PASSWORD)
TENANT_A_ID = "test-tenant-001"


def auth_header(email, password):
    return {"Authorization": f"Bearer {get_id_token(email, password)}"}


def create_regulation(business_unit_ids=("BU1", "BU2")) -> dict:
    resp = httpx.post(
        f"{BASE_URL}/regulations",
        json={"title": f"{TITLE_PREFIX}{uuid.uuid4().hex[:8]}", "due_date": "2027-06-30", "domain": "Batteries",
              "business_unit_ids": list(business_unit_ids), "add_to_roadmap_dashboard": False},
        headers=auth_header(*TENANT_A_ADMIN), timeout=TIMEOUT,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def save(reg_id, bu, body, headers):
    return httpx.patch(f"{BASE_URL}/regulations/{reg_id}/bu-status/{bu}", json=body, headers=headers, timeout=TIMEOUT)


def activity(reg_id, headers, bu=None):
    params = {"bu_code": bu} if bu else None
    resp = httpx.get(f"{BASE_URL}/regulations/{reg_id}/activity", params=params, headers=headers, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    return resp.json()


def upload(reg_id, bu, headers):
    return httpx.post(
        f"{BASE_URL}/regulations/{reg_id}/documents", data={"bu_code": bu},
        files={"file": ("supplier-declaration.txt", io.BytesIO(b"T065 evidence"), "text/plain")},
        headers=headers, timeout=TIMEOUT,
    )


def remove(reg_id, entry_id, headers):
    return httpx.delete(f"{BASE_URL}/regulations/{reg_id}/activity/{entry_id}", headers=headers, timeout=TIMEOUT)


def unit_note(reg_id, bu):
    data = db.collection("tenants").document(TENANT_A_ID).collection("regulations").document(reg_id).get().to_dict()
    return ((data.get("buStatuses") or {}).get(bu) or {}).get("note")


def test_activity_requires_token():
    assert httpx.get(f"{BASE_URL}/regulations/x/activity", timeout=TIMEOUT).status_code == 401
    assert httpx.delete(f"{BASE_URL}/regulations/x/activity/y", timeout=TIMEOUT).status_code == 401


def test_me_carries_name_and_email():
    me = httpx.get(f"{BASE_URL}/me", headers=auth_header(*TENANT_A_USER), timeout=TIMEOUT).json()
    assert "displayName" in me and me["email"] == "user@testtenant.com"


def test_status_note_and_document_saved_as_one_entry():
    reg = create_regulation()
    user = auth_header(*TENANT_A_USER)
    doc = upload(reg["id"], "BU1", user).json()
    resp = save(reg["id"], "BU1", {"status": "action_needed", "note": "Supplier late", "document_keys": [doc["key"]]}, user)
    assert resp.status_code == 200, resp.text

    entries = activity(reg["id"], user, "BU1")
    assert len(entries) == 1  # the document is part of the entry, not shown again on its own
    e = entries[0]
    assert e["statusChanged"] and e["status"] == "action_needed" and e["note"] == "Supplier late"
    assert [d["key"] for d in e["documents"]] == [doc["key"]]
    assert e["actorEmail"] == "user@testtenant.com" and e["canRemove"] is False
    assert unit_note(reg["id"], "BU1") == "Supplier late"


def test_any_role_sees_every_units_history_newest_first():
    reg = create_regulation()
    admin, user = auth_header(*TENANT_A_ADMIN), auth_header(*TENANT_A_USER)
    assert save(reg["id"], "BU2", {"status": "pending"}, admin).status_code == 200
    assert save(reg["id"], "BU2", {"note": "Reprint ordered"}, admin).status_code == 200
    entries = activity(reg["id"], user)  # User (BU1) reads BU2's history
    assert [e["note"] for e in entries] == ["Reprint ordered", ""]
    assert all(e["buCode"] == "BU2" for e in entries)


def test_same_status_and_no_note_records_nothing():
    reg = create_regulation()
    admin = auth_header(*TENANT_A_ADMIN)
    save(reg["id"], "BU1", {"status": "pending"}, admin)
    save(reg["id"], "BU1", {"status": "pending"}, admin)
    assert len(activity(reg["id"], admin, "BU1")) == 1


def test_document_of_another_unit_rejected():
    reg = create_regulation()
    admin = auth_header(*TENANT_A_ADMIN)
    doc = upload(reg["id"], "BU2", admin).json()
    resp = save(reg["id"], "BU1", {"note": "x", "document_keys": [doc["key"]]}, admin)
    assert resp.status_code == 400


def test_upload_without_save_still_shows_in_history():
    reg = create_regulation()
    user = auth_header(*TENANT_A_USER)
    doc = upload(reg["id"], "BU1", user).json()
    entries = activity(reg["id"], user, "BU1")
    assert len(entries) == 1 and entries[0]["legacy"] and entries[0]["documents"][0]["key"] == doc["key"]


def test_remove_own_note_softly_and_unit_falls_back_to_previous_note():
    reg = create_regulation()
    user = auth_header(*TENANT_A_USER)
    save(reg["id"], "BU1", {"note": "First note"}, user)
    save(reg["id"], "BU1", {"note": "Second note"}, user)
    newest = activity(reg["id"], user, "BU1")[0]
    assert newest["note"] == "Second note" and newest["canRemove"]

    assert remove(reg["id"], newest["id"], user).status_code == 200
    assert [e["note"] for e in activity(reg["id"], user, "BU1")] == ["First note"]
    assert unit_note(reg["id"], "BU1") == "First note"
    stored = (db.collection("tenants").document(TENANT_A_ID).collection("regulations").document(reg["id"])
              .collection("activity").document(newest["id"]).get().to_dict())
    assert stored["removed"] is True and stored["note"] == "Second note"  # kept on the server
    assert remove(reg["id"], newest["id"], user).status_code == 404


def test_cannot_remove_someone_elses_note_or_a_status_change():
    reg = create_regulation()
    admin, user = auth_header(*TENANT_A_ADMIN), auth_header(*TENANT_A_USER)
    save(reg["id"], "BU1", {"note": "Admin note"}, admin)
    save(reg["id"], "BU1", {"status": "compliant", "note": "Done"}, user)
    by_note = {e["note"]: e for e in activity(reg["id"], user, "BU1")}
    assert remove(reg["id"], by_note["Admin note"]["id"], user).status_code == 403
    assert remove(reg["id"], by_note["Done"]["id"], user).status_code == 400


def test_other_tenant_cannot_read_or_remove():
    reg = create_regulation()
    save(reg["id"], "BU1", {"note": "Private"}, auth_header(*TENANT_A_ADMIN))
    entry_id = activity(reg["id"], auth_header(*TENANT_A_ADMIN))[0]["id"]
    other = auth_header(*TENANT_B_ADMIN)
    assert httpx.get(f"{BASE_URL}/regulations/{reg['id']}/activity", headers=other, timeout=TIMEOUT).status_code == 404
    assert remove(reg["id"], entry_id, other).status_code == 404
