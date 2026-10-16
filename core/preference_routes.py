"""
T020 - Saved Dashboard view (DL-8, Srinivas Oct 16 2026): "The default view
can be the last view that the users looked at before they logged out."

The Dashboard keeps its whole view - By regulation / By Business Unit,
filters and sort - in the URL query string, so the saved view IS that query
string. Stored per user, on the server, so it follows the person to any
computer.

Stored at tenants/{tenantId}/userPreferences/{uid}, NOT on the users/{uid}
directory record: GET /users lists every document in the users collection,
so a preference written for an account without a directory record would
otherwise appear as an empty People Directory entry.

A preference is not compliance data (no regulation, status, note or
document changes), so it is not written to the audit log.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.deps import RequestContext, get_current_context

router = APIRouter()

VIEW_MAX_LENGTH = 2000


class DashboardViewRequest(BaseModel):
    view: str


def _pref_ref(ctx: RequestContext):
    return ctx.tenant_ref.collection("userPreferences").document(ctx.uid)


@router.get("/me/dashboard-view")
def get_dashboard_view(ctx: RequestContext = Depends(get_current_context)):
    """The caller's own saved view, or "" if none. Never another user's."""
    snap = _pref_ref(ctx).get()
    data = snap.to_dict() if snap.exists else {}
    return {"view": data.get("dashboardView", "")}


@router.put("/me/dashboard-view")
def put_dashboard_view(
    payload: DashboardViewRequest,
    ctx: RequestContext = Depends(get_current_context),
):
    view = payload.view.strip().lstrip("?")
    if len(view) > VIEW_MAX_LENGTH:
        raise HTTPException(status_code=400, detail=f"View must be {VIEW_MAX_LENGTH} characters or fewer.")
    if any(c in view for c in "\r\n\t"):
        raise HTTPException(status_code=400, detail="View must be a single-line query string.")

    _pref_ref(ctx).set(
        {"dashboardView": view, "dashboardViewUpdatedAt": datetime.now(timezone.utc)},
        merge=True,
    )
    return {"view": view}
