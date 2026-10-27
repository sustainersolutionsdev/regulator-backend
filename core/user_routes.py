"""
core/user_routes.py - Users / Directory actions beyond Add Users.

POST /users/{uid}/resend-invitation (T061, approved Oct 26): Admin/SME send
a fresh invitation email to someone who missed it or whose 1-hour link
expired. Only for people in the caller's own company who haven't been
removed; anything else gets the same 404 (NFR-1).
"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from core.deps import RequestContext, get_current_context, require_admin_or_sme
from core.invitations import schedule_invitation

router = APIRouter()


@router.post("/users/{uid}/resend-invitation")
def resend_invitation(
    uid: str,
    background_tasks: BackgroundTasks,
    ctx: RequestContext = Depends(get_current_context),
):
    require_admin_or_sme(ctx)
    snap = ctx.tenant_ref.collection("users").document(uid).get()
    person = (snap.to_dict() or {}) if snap.exists else {}
    if not snap.exists or person.get("hidden") or not person.get("email"):
        raise HTTPException(status_code=404, detail="User not found.")
    schedule_invitation(background_tasks, ctx.tenant_id, uid, ctx.uid, resend=True)
    return {"message": f"Invitation email sent to {person['email']}.", "uid": uid}
