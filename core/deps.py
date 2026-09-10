from fastapi import Header, HTTPException, Depends
from firebase_admin import auth
from google.cloud import firestore
from typing import Optional

db = firestore.Client(project="regulator-dev")


class RequestContext:
    """
    Carries the verified identity for this request. Every field here
    comes from the verified token - never from a client-supplied
    parameter - per TAD 6.1.
    """
    def __init__(self, uid: str, tenant_id: str, role: str, business_unit_ids: list[str]):
        self.uid = uid
        self.tenant_id = tenant_id
        self.role = role
        self.business_unit_ids = business_unit_ids
        self.tenant_ref = db.collection("tenants").document(tenant_id)


def get_current_context(authorization: Optional[str] = Header(None)) -> RequestContext:
    """
    Verifies the Firebase ID token from the Authorization header and
    builds a tenant-scoped RequestContext. Rejects the request outright
    if the token is missing, invalid, or has no tenant_id claim -
    there is no fallback to an "all tenants" query.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header.")

    id_token = authorization.split("Bearer ")[1]

    try:
        decoded = auth.verify_id_token(id_token)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"DEBUG: {type(e).__name__}: {str(e)}")

    tenant_id = decoded.get("tenant_id")
    role = decoded.get("role")
    business_unit_ids = decoded.get("businessUnitIds", [])

    if not tenant_id or not role:
        raise HTTPException(status_code=403, detail="Token missing tenant_id or role claim.")

    return RequestContext(
        uid=decoded["uid"],
        tenant_id=tenant_id,
        role=role,
        business_unit_ids=business_unit_ids,
    )

def require_bu_write_access(bu_code: str, ctx: RequestContext) -> None:
    """
    Enforces FR-0.2 write scoping. Admin/SME write everywhere in their
    tenant; User is restricted to their assigned Business Unit(s).
    Raises an explicit 403 with a clear body - never a silent no-op -
    so the frontend can render FR-0.2's required on-screen error message.
    """
    if ctx.role in ("admin", "sme"):
        return
    if bu_code not in ctx.business_unit_ids:
        raise HTTPException(
            status_code=403,
            detail=f"You are not permitted to edit Business Unit '{bu_code}'.",
        )


def require_admin_or_sme(ctx: RequestContext) -> None:
    """
    Gates tenant-level configuration actions (like creating a Business
    Unit) that aren't scoped to an existing bu_code, so
    require_bu_write_access doesn't apply. Per OPEN-1 (closed), Admin
    and SME are treated identically — no functional boundary.
    """
    if ctx.role not in ("admin", "sme"):
        raise HTTPException(
            status_code=403,
            detail="Only Admin or SME can configure Business Units.",
        )        