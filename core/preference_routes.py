"""
Per-user preferences, stored at tenants/{tenantId}/userPreferences/{uid}.

T020 - Saved Dashboard view (DL-8, Srinivas Oct 16 2026): "The default view
can be the last view that the users looked at before they logged out."
The Dashboard keeps its whole view - By regulation / By Business Unit,
filters and sort - in the URL query string, so the saved view IS that query
string. Stored per user, on the server, so it follows the person to any
computer.

T025 - Email alerts on/off (DL-9, Srinivas Oct 19 2026): "Can we give the
option to turn it off if the users do not want to receive emails?" Each
person turns alerts off (and back on) for themselves only. On by default:
no saved value means on. core/alerts.py reads `emailAlerts` when choosing
who to email.

T026 - Turning alerts back ON also lifts any block at Brevo. Brevo adds a
List-Unsubscribe header to every email, so the inbox shows its own
"Unsubscribe" link, and using it blocks the address at Brevo, outside the
app. Lifting it when the person turns alerts on keeps this setting the one
that decides (core/alerts.py resubscribe_at_provider; send mode only).

Stored here, NOT on the users/{uid} directory record: GET /users lists every
document in the users collection, so a preference written for an account
without a directory record would otherwise appear as an empty People
Directory entry.

Preferences are not compliance data (no regulation, status, note or
document changes), so they are not written to the audit log. Each change
keeps its own timestamp, and every alert records who was skipped because
alerts were off (Alerts Log).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, StrictBool

from core.deps import RequestContext, get_current_context

router = APIRouter()

VIEW_MAX_LENGTH = 2000


class DashboardViewRequest(BaseModel):
    view: str


class EmailAlertsRequest(BaseModel):
    # StrictBool: "false" or 0 is rejected (422) rather than read as a bool.
    enabled: StrictBool


def _pref_ref(ctx: RequestContext):
    return ctx.tenant_ref.collection("userPreferences").document(ctx.uid)


def _prefs(ctx: RequestContext) -> dict:
    snap = _pref_ref(ctx).get()
    return snap.to_dict() if snap.exists else {}


# --- T020: saved Dashboard view --------------------------------------------------

@router.get("/me/dashboard-view")
def get_dashboard_view(ctx: RequestContext = Depends(get_current_context)):
    """The caller's own saved view, or "" if none. Never another user's."""
    return {"view": _prefs(ctx).get("dashboardView", "")}


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


# --- T025: email alerts on/off (DL-9) --------------------------------------------

@router.get("/me/email-alerts")
def get_email_alerts(ctx: RequestContext = Depends(get_current_context)):
    """The caller's own setting. On unless they turned it off."""
    return {"enabled": _prefs(ctx).get("emailAlerts") is not False}


@router.put("/me/email-alerts")
def put_email_alerts(
    payload: EmailAlertsRequest,
    background_tasks: BackgroundTasks,
    ctx: RequestContext = Depends(get_current_context),
):
    _pref_ref(ctx).set(
        {"emailAlerts": payload.enabled, "emailAlertsUpdatedAt": datetime.now(timezone.utc)},
        merge=True,
    )
    if payload.enabled:
        record = ctx.tenant_ref.collection("users").document(ctx.uid).get()
        email = (record.to_dict() or {}).get("email") if record.exists else None
        if email:
            from core.alerts import resubscribe_at_provider
            background_tasks.add_task(resubscribe_at_provider, email)
    return {"enabled": payload.enabled}
