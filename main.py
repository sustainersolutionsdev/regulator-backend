from dotenv import load_dotenv
load_dotenv()
import firebase_admin
from firebase_admin import credentials
import os

cred = credentials.Certificate(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
firebase_admin.initialize_app(cred)

from fastapi import FastAPI, Depends
from core.deps import get_current_context, require_bu_write_access, RequestContext

app = FastAPI()


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