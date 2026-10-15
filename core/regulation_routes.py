"""
T014 - Edit and remove a regulation (FR-1.4, NFR-2, NFR-8).
T018 - Compliance status and its explanation (FR-2.1, FR-2.5).

Decided by Srinivas, Oct 13 2026 (reply to the Roadmap editing questions):
  1. Main details (title, abbreviated title, due date, domain, country,
     region, business units) can be changed by Admin and SME only.
  2. Users change nothing from the Roadmap; their updates happen on the
     Dashboard only (T020).
  3. Only Admin and SME can remove a regulation.

So both endpoints here are Admin/SME-only (require_admin_or_sme), and the
BU-scoped User edit rule (FR-0.2 / FR-1.4 as originally written) now applies
to the Dashboard edit path only.

T018: `status` is one of "compliant" / "pending" / "action_needed", or null
for "not set". It is always set by a person (FR-2.1: never inferred or
AI-generated) and is never defaulted on create. `status_note` is the
optional explanation, authored by Admin/SME. A status change also records
statusUpdatedAt / statusUpdatedBy, which the AI red-status summaries (M4)
and the update alerts (M3) can use without reading the audit log.

"Remove" is a soft delete (NFR-8): hidden / hiddenAt / hiddenBy are set, the
record is never erased, and both writes go through write_with_audit() so
every change lands in the audit log in the same transaction.

Kept in its own module and mounted with app.include_router() so main.py,
which has caused merge trouble before, only gains two lines.
"""
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.audit import write_with_audit
from core.deps import RequestContext, get_current_context, require_admin_or_sme
from core.tenancy import db

router = APIRouter()

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# T018: the three SME-set statuses. None (null) means "not set".
STATUS_VALUES = ("compliant", "pending", "action_needed")
STATUS_NOTE_MAX = 1000

# API field (snake_case) -> stored Firestore field (camelCase), same mapping
# as create_regulation() in core/tenancy.py.
EDITABLE_FIELDS = {
    "title": "title",
    "abbreviated_title": "abbreviatedTitle",
    "due_date": "dueDate",
    "domain": "domain",
    "country": "country",
    "region": "region",
    "business_unit_ids": "businessUnitIds",
    "notes": "notes",
    "add_to_roadmap_dashboard": "addToRoadmapDashboard",
    "status": "status",
    "status_note": "statusNote",
}
REQUIRED_NOT_BLANK = {"title", "due_date", "domain", "business_unit_ids"}


class UpdateRegulationRequest(BaseModel):
    """Every field optional: only the fields sent are changed."""
    title: Optional[str] = None
    abbreviated_title: Optional[str] = None
    due_date: Optional[str] = None
    domain: Optional[str] = None
    country: Optional[str] = None
    region: Optional[str] = None
    business_unit_ids: Optional[list[str]] = None
    notes: Optional[str] = None
    add_to_roadmap_dashboard: Optional[bool] = None
    status: Optional[str] = None       # T018: null clears it ("not set")
    status_note: Optional[str] = None  # T018: optional explanation


def _get_live_regulation(ctx: RequestContext, reg_id: str):
    """
    Returns (doc_ref, data) for a non-hidden regulation in the CALLER's tenant.
    A regulation in another tenant, a hidden one, or a missing one all return
    the same 404, so the response never confirms that another tenant's ID exists.
    """
    reg_ref = ctx.tenant_ref.collection("regulations").document(reg_id)
    snap = reg_ref.get()
    if not snap.exists or snap.to_dict().get("hidden", False):
        raise HTTPException(status_code=404, detail="Regulation not found.")
    return reg_ref, snap.to_dict()


@router.patch("/regulations/{reg_id}")
def update_regulation(
    reg_id: str,
    payload: UpdateRegulationRequest,
    ctx: RequestContext = Depends(get_current_context),
):
    require_admin_or_sme(ctx)
    reg_ref, current = _get_live_regulation(ctx, reg_id)

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No fields to update.")

    errors = []
    for field in REQUIRED_NOT_BLANK & changes.keys():
        value = changes[field]
        if value is None or (isinstance(value, str) and not value.strip()) or value == []:
            errors.append(f"'{field}' cannot be blank")
    if changes.get("due_date") and not DATE_RE.match(changes["due_date"].strip()):
        errors.append("'due_date' must be YYYY-MM-DD")
    if "status" in changes and changes["status"] is not None and changes["status"] not in STATUS_VALUES:
        errors.append("'status' must be one of: compliant, pending, action_needed (or null for not set)")
    if changes.get("status_note") and len(changes["status_note"].strip()) > STATUS_NOTE_MAX:
        errors.append(f"'status_note' must be {STATUS_NOTE_MAX} characters or fewer")
    if errors:
        raise HTTPException(status_code=400, detail=errors)

    now = datetime.now(timezone.utc)
    update = {}
    for api_field, value in changes.items():
        if value is None and api_field != "status":
            # Optional text fields sent as null are cleared to "".
            # (status keeps null: it means "not set".)
            value = False if api_field == "add_to_roadmap_dashboard" else ""
        if isinstance(value, str):
            value = value.strip()
        update[EDITABLE_FIELDS[api_field]] = value
    if "status" in changes and changes["status"] != current.get("status"):
        update["statusUpdatedAt"] = now
        update["statusUpdatedBy"] = ctx.uid
    update["lastUpdated"] = now
    update["lastUpdatedBy"] = ctx.uid

    write_with_audit(
        tenant_id=ctx.tenant_id,
        doc_ref=reg_ref,
        data=update,
        actor_uid=ctx.uid,
        actor_role=ctx.role,
        action="update_regulation",
    )
    return {"id": reg_id, **current, **update}


@router.delete("/regulations/{reg_id}")
def remove_regulation(
    reg_id: str,
    ctx: RequestContext = Depends(get_current_context),
):
    """Soft delete (NFR-8): hidden from every screen, never erased."""
    require_admin_or_sme(ctx)
    reg_ref, _ = _get_live_regulation(ctx, reg_id)

    now = datetime.now(timezone.utc)
    write_with_audit(
        tenant_id=ctx.tenant_id,
        doc_ref=reg_ref,
        data={"hidden": True, "hiddenAt": now, "hiddenBy": ctx.uid, "lastUpdated": now},
        actor_uid=ctx.uid,
        actor_role=ctx.role,
        action="hide_regulation",
    )
    return {"id": reg_id, "hidden": True}
