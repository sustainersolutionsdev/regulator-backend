"""
core/business_unit_routes.py - editing Business Units (Srinivas, Oct 24 US:
"There has to be a way to edit/change BUs"; testing feedback, Oct 26).

PATCH /business-units/{bu_code} renames a unit: the label changes, the code
stays. The code is what regulations, per-unit statuses (DL-8), documents
and Users' assignments refer to, so keeping it means nothing else has to
move and every screen shows the new name straight away.

Deactivating a unit is NOT here: what it should do to that unit's
regulations, statuses and Users is Srinivas's decision (OPEN-18). Units are
never deleted (NFR-8).

Admin/SME only (a tenant-configuration action, like adding a unit), scoped
to the caller's own tenant, and audited like every other change (NFR-2).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.audit import write_with_audit
from core.deps import RequestContext, get_current_context, require_admin_or_sme

router = APIRouter()

MAX_LABEL_LENGTH = 80


class RenameBusinessUnitRequest(BaseModel):
    label: str


@router.patch("/business-units/{bu_code}")
def rename_business_unit(
    bu_code: str,
    payload: RenameBusinessUnitRequest,
    ctx: RequestContext = Depends(get_current_context),
):
    require_admin_or_sme(ctx)
    label = " ".join(payload.label.split())
    if not label:
        raise HTTPException(status_code=400, detail="Enter a name for the Business Unit.")
    if len(label) > MAX_LABEL_LENGTH:
        raise HTTPException(status_code=400, detail=f"Keep the name to {MAX_LABEL_LENGTH} characters or fewer.")

    bu_ref = ctx.tenant_ref.collection("businessUnits").document(bu_code)
    snapshot = bu_ref.get()
    if not snapshot.exists:
        # Same answer for "not in your company" and "doesn't exist" (NFR-1).
        raise HTTPException(status_code=404, detail=f"Business Unit '{bu_code}' not found.")

    current = snapshot.to_dict() or {}
    if current.get("label") == label:
        return {"id": bu_code, **current}

    write_with_audit(
        tenant_id=ctx.tenant_id,
        doc_ref=bu_ref,
        data={"label": label},
        actor_uid=ctx.uid,
        actor_role=ctx.role,
        action="rename_business_unit",
    )
    return {"id": bu_code, **current, "label": label}
