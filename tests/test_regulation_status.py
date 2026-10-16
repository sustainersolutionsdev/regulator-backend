"""
T020 - Compliance status per Business Unit (DL-8, Srinivas Oct 16 2026).

Replaces T018's single-status tests. Rules:
  - Each Business Unit on a regulation has its own status and explanation
    (buStatuses); a shared regulation can be red for one unit and green
    for another. No combined status is stored.
  - PATCH /regulations/{id}/bu-status/{bu_code}: Admin/SME any unit; User
    only their own unit(s), else 403 with a message the Dashboard shows.
  - The unit must be one the regulation is tagged to.
  - status is compliant / pending / action_needed, or null for "not set";
    never defaulted. note is the explanation, max 1,000 characters.
  - Users cannot edit the SME's main Notes (general PATCH is Admin/SME),
    and the general PATCH no longer accepts a status at all.
  - Audited (NFR-2); never crosses tenants (NFR-1).

Titles use "T018 Status Regulation " (already registered in
tests/cleanup_test_data.py) so the session cleanup removes them.
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


def create_regulation(business_unit_ids=("BU1", "BU2")) -> dict:
    resp = httpx.post(
        f"{BASE_URL}/regulations",
        json={
            "title": f"{TITLE_PREFIX}{uuid.uuid4().hex[:8]}",
            "due_date": "2027-06-30",
            "domain": "Batteries",
            "business_unit_ids": list(business_unit_ids),
            "notes": "SME main notes",
            "add_to_roadmap_dashboard": False,
        },
        headers=auth_header(*TENANT_A_ADMIN),
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def set_bu(reg_id, bu, body, headers):
    return httpx.patch(f"{BASE_URL}/regulations/{reg_id}/bu-status/{bu}", json=body, headers=headers, timeout=TIMEOUT)


def stored(reg_id: str) -> dict:
    return (
        db.collection("tenants").document(TENANT_A_ID)
        .collection("regulations").document(reg_id).get().to_dict()
    )


def audit_actions(reg_id: str) -> list[str]:
    docs = (
        db.collection("tenants").document(TENANT_A_ID).collection("auditLog")
        .where("targetPath", "==", f"tenants/{TENANT_A_ID}/regulations/{reg_id}")
        .stream()
    )
    return [d.to_dict()["action"] for d in docs]


def test_bu_status_requires_token():
    assert httpx.patch(f"{BASE_URL}/regulations/x/bu-status/BU1", json={"status": "pending"}, timeout=TIMEOUT).status_code == 401


def test_new_regulation_has_no_status():
    """Never defaulted: every unit is "not set" until a person rates it."""
    reg = create_regulation()
    data = stored(reg["id"])
    assert not data.get("buStatuses")
    assert data.get("status") is None


def test_general_edit_no_longer_accepts_status():
    """T018's single status is retired; status goes through bu-status only."""
    reg = create_regulation()
    resp = httpx.patch(
        f"{BASE_URL}/regulations/{reg['id']}", json={"status": "compliant"},
        headers=auth_header(*TENANT_A_ADMIN), timeout=TIMEOUT,
    )
    assert resp.status_code == 400  # nothing it recognises: "No fields to update."
    assert stored(reg["id"]).get("status") is None


def test_user_sets_own_unit_status_and_explanation_audited():
    reg = create_regulation()
    user = auth_header(*TENANT_A_USER)
    my_uid = httpx.get(f"{BASE_URL}/me", headers=user, timeout=TIMEOUT).json()["uid"]

    resp = set_bu(reg["id"], "BU1", {"status": "action_needed", "note": "  Labelling gap  "}, user)
    assert resp.status_code == 200, resp.text

    entry = stored(reg["id"])["buStatuses"]["BU1"]
    assert entry["status"] == "action_needed"
    assert entry["note"] == "Labelling gap"
    assert entry["updatedBy"] == my_uid
    assert entry["statusChangedAt"]
    assert "BU2" not in stored(reg["id"])["buStatuses"]
    assert "update_bu_status" in audit_actions(reg["id"])


def test_user_cannot_set_another_units_status():
    reg = create_regulation()
    resp = set_bu(reg["id"], "BU2", {"status": "compliant"}, auth_header(*TENANT_A_USER))
    assert resp.status_code == 403
    assert "not permitted to edit Business Unit 'BU2'" in resp.json()["detail"]
    assert "BU2" not in (stored(reg["id"]).get("buStatuses") or {})


def test_units_on_a_shared_regulation_keep_separate_statuses():
    """Srinivas's example: red for one unit, green for another."""
    reg = create_regulation()
    assert set_bu(reg["id"], "BU1", {"status": "action_needed", "note": "BU1 gap"}, auth_header(*TENANT_A_USER)).status_code == 200
    assert set_bu(reg["id"], "BU2", {"status": "compliant", "note": "BU2 done"}, auth_header(*TENANT_A_SME)).status_code == 200

    statuses = stored(reg["id"])["buStatuses"]
    assert statuses["BU1"]["status"] == "action_needed" and statuses["BU1"]["note"] == "BU1 gap"
    assert statuses["BU2"]["status"] == "compliant" and statuses["BU2"]["note"] == "BU2 done"


def test_user_sees_every_units_status_in_list():
    """Viewing is tenant-wide: a BU1 User sees BU2's status too."""
    reg = create_regulation()
    set_bu(reg["id"], "BU2", {"status": "pending", "note": "Awaiting supplier"}, auth_header(*TENANT_A_ADMIN))
    listed = httpx.get(f"{BASE_URL}/regulations", headers=auth_header(*TENANT_A_USER), timeout=TIMEOUT).json()
    row = next(r for r in listed if r["id"] == reg["id"])
    assert row["buStatuses"]["BU2"]["status"] == "pending"
    assert row["buStatuses"]["BU2"]["note"] == "Awaiting supplier"


def test_unit_must_be_on_the_regulation():
    reg = create_regulation(business_unit_ids=("BU1",))
    resp = set_bu(reg["id"], "BU2", {"status": "pending"}, auth_header(*TENANT_A_ADMIN))
    assert resp.status_code == 400
    assert "not tagged to Business Unit 'BU2'" in resp.json()["detail"]


def test_invalid_status_and_long_note_rejected():
    reg = create_regulation()
    admin = auth_header(*TENANT_A_ADMIN)
    for bad in ("green", "Compliant", "red", ""):
        resp = set_bu(reg["id"], "BU1", {"status": bad}, admin)
        assert resp.status_code == 400, f"{bad!r} should be rejected"
    resp = set_bu(reg["id"], "BU1", {"status": "pending", "note": "x" * 1001}, admin)
    assert resp.status_code == 400
    assert set_bu(reg["id"], "BU1", {}, admin).status_code == 400
    assert not stored(reg["id"]).get("buStatuses")


def test_clearing_status_keeps_explanation():
    reg = create_regulation()
    admin = auth_header(*TENANT_A_ADMIN)
    set_bu(reg["id"], "BU1", {"status": "pending", "note": "Awaiting supplier data"}, admin)
    resp = set_bu(reg["id"], "BU1", {"status": None}, admin)
    assert resp.status_code == 200, resp.text
    entry = stored(reg["id"])["buStatuses"]["BU1"]
    assert entry["status"] is None
    assert entry["note"] == "Awaiting supplier data"


def test_explanation_only_change_keeps_status_changed_time():
    reg = create_regulation()
    admin = auth_header(*TENANT_A_ADMIN)
    set_bu(reg["id"], "BU1", {"status": "compliant"}, admin)
    first = stored(reg["id"])["buStatuses"]["BU1"]["statusChangedAt"]

    assert set_bu(reg["id"], "BU1", {"note": "Evidence on file"}, admin).status_code == 200
    entry = stored(reg["id"])["buStatuses"]["BU1"]
    assert entry["statusChangedAt"] == first
    assert entry["status"] == "compliant"
    assert entry["updatedAt"] > first


def test_user_cannot_edit_sme_main_notes():
    """Srinivas 2A: Users edit the status explanation only."""
    reg = create_regulation(business_unit_ids=("BU1",))
    resp = httpx.patch(
        f"{BASE_URL}/regulations/{reg['id']}", json={"notes": "user rewrite"},
        headers=auth_header(*TENANT_A_USER), timeout=TIMEOUT,
    )
    assert resp.status_code == 403
    assert stored(reg["id"])["notes"] == "SME main notes"


def test_other_tenant_cannot_set_status():
    reg = create_regulation()
    resp = set_bu(reg["id"], "BU1", {"status": "compliant"}, auth_header(*TENANT_B_ADMIN))
    assert resp.status_code == 404
    assert not stored(reg["id"]).get("buStatuses")
