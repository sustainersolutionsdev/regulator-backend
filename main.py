from dotenv import load_dotenv
load_dotenv()
import firebase_admin
from firebase_admin import credentials
import os
from core.deps import get_current_context, require_bu_write_access, require_admin_or_sme, RequestContext
from fastapi import FastAPI, Depends, HTTPException

cred = credentials.Certificate(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
firebase_admin.initialize_app(cred)

from fastapi.middleware.cors import CORSMiddleware
from core.deps import get_current_context, require_bu_write_access, require_admin_or_sme, RequestContext

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/me")
def whoami(ctx: RequestContext = Depends(get_current_context)):
    """Sanity endpoint — proves the middleware correctly decodes the token."""
    return {
        "uid": ctx.uid,
        "tenant_id": ctx.tenant_id,
        "role": ctx.role,
        "businessUnitIds": ctx.business_unit_ids,
    }


@app.post("/test-write/{bu_code}")
def test_write(bu_code: str, ctx: RequestContext = Depends(get_current_context)):
    """Proves the 403 enforcement path works for out-of-scope BU writes."""
    require_bu_write_access(bu_code, ctx)
    return {"message": f"Write to '{bu_code}' allowed for role '{ctx.role}'."}

@app.get("/users")
def list_users(ctx: RequestContext = Depends(get_current_context)):
    """
    Lists users in the CALLER's tenant only. ctx.tenant_ref is
    pre-scoped from the verified token — there is no parameter
    here a caller could manipulate to reach another tenant.
    """
    docs = ctx.tenant_ref.collection("users").stream()
    return [{"id": d.id, **d.to_dict()} for d in docs]

from core.tenancy import create_business_unit, list_business_units

@app.get("/business-units")
def get_business_units(ctx: RequestContext = Depends(get_current_context)):
    """
    Any authenticated role can view all Business Units in their own
    tenant (FR-0.2: viewing is tenant-wide for every role; only
    editing is BU-scoped).
    """
    return list_business_units(ctx.tenant_id)


@app.post("/business-units")
def post_business_unit(
    bu_code: str,
    label: str,
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Creates a new Business Unit in the caller's tenant. Admin/SME only
    (FR-0.2/FR-0.3) — this is a tenant-configuration action, not a
    write to an existing BU, so it uses require_admin_or_sme rather
    than require_bu_write_access.
    """
    require_admin_or_sme(ctx)
    try:
        create_business_unit(ctx.tenant_id, bu_code, label)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"message": f"Business Unit '{bu_code}' created.", "code": bu_code, "label": label}    