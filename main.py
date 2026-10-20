from dotenv import load_dotenv
load_dotenv()
import firebase_admin
from firebase_admin import credentials
import os
from core.deps import get_current_context, require_bu_write_access, require_admin_or_sme, RequestContext
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, BackgroundTasks
from pydantic import BaseModel
from typing import Optional
from firebase_admin import auth as firebase_auth
from core.users import create_user_and_get_reset_link
from core.tenancy import create_regulation, validate_regulation_payload, list_regulations
from core.storage import upload_document, BUCKET_NAME

if not firebase_admin._apps:
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
    """Sanity endpoint â€” proves the middleware correctly decodes the token."""
    return {
        "uid": ctx.uid,
        "tenant_id": ctx.tenant_id,
        "role": ctx.role,
        "businessUnitIds": ctx.business_unit_ids,
    }


class AddUserRequest(BaseModel):
    email: str
    display_name: str = ""
    title: Optional[str] = ""
    role: str
    business_unit_ids: list[str] = []
    notes: Optional[str] = ""


@app.post("/users")
def add_user(payload: AddUserRequest, ctx: RequestContext = Depends(get_current_context)):
    """
    FR-0.4: Admin/SME add a new user via name, email, title, role, BU
    assignment, notes (title/notes confirmed against Srinivas's Add Users
    doc, 9/16 â€” role and multi-BU confirmed unchanged in the same reply).
    Gated like POST /business-units â€” a tenant-configuration action, not
    a write to an existing BU, so it uses require_admin_or_sme rather
    than require_bu_write_access.
    """
    require_admin_or_sme(ctx)

    if payload.role not in ("admin", "sme", "user"):
        raise HTTPException(status_code=400, detail=f"Invalid role '{payload.role}'.")

    # FR-0.4: User role must carry at least one specific BU; Admin/SME
    # get auto-scoped to all BUs by create_user() regardless of input.
    if payload.role == "user" and not payload.business_unit_ids:
        raise HTTPException(
            status_code=400,
            detail="User role requires at least one Business Unit assignment.",
        )

    try:
        uid, reset_link = create_user_and_get_reset_link(
            email=payload.email,
            tenant_id=ctx.tenant_id,
            role=payload.role,
            business_unit_ids=payload.business_unit_ids,
            display_name=payload.display_name,
            title=payload.title,
            notes=payload.notes,
        )
    except firebase_auth.EmailAlreadyExistsError:
        raise HTTPException(
            status_code=409,
            detail=f"A user with email '{payload.email}' already exists.",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "message": f"User '{payload.email}' created.",
        "uid": uid,
        "role": payload.role,
        "reset_link": reset_link,
    }    

@app.get("/users")
def list_users(ctx: RequestContext = Depends(get_current_context)):
    """
    Lists users in the CALLER's tenant only. ctx.tenant_ref is
    pre-scoped from the verified token â€” there is no parameter
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
    (FR-0.2/FR-0.3) â€” this is a tenant-configuration action, not a
    write to an existing BU, so it uses require_admin_or_sme rather
    than require_bu_write_access.
    """
    require_admin_or_sme(ctx)
    try:
        create_business_unit(ctx.tenant_id, bu_code, label)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"message": f"Business Unit '{bu_code}' created.", "code": bu_code, "label": label}    


class DocumentMetadata(BaseModel):
    """Shape returned by POST /regulations/upload-document â€” passed back
    in as-is when creating the regulation record."""
    file_name: str
    gcs_uri: str
    content_type: str
    size_bytes: int
    uploaded_by: str
    uploaded_at: str


class AddRegulationRequest(BaseModel):
    """
    Corrected against Srinivas's actual "Input form to Add Regulation"
    doc â€” NOT the frozen Requirements Document's FR-0.7, which drifted
    from this source and has been superseded for this field list.
    Dropped entirely (no FR-0.7 trace kept): cfr_citation, agency,
    regulatory_focus, compliance_dates, status, lifecycle_stage,
    source_url, tags.
    """
    title: str
    abbreviated_title: Optional[str] = ""
    due_date: str
    domain: str  # singular per the iWant doc's "Domain:" â€” not multi-select
    country: Optional[str] = ""
    region: Optional[str] = ""
    business_unit_ids: list[str]
    notes: Optional[str] = ""
    documents: Optional[list[DocumentMetadata]] = []
    add_to_roadmap_dashboard: bool = False


@app.post("/regulations/upload-document")
async def upload_regulation_document(
    file: UploadFile = File(...),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Step 1 of the two-step add-regulation-with-document flow (same shape
    as any "upload then reference" pattern, reimplemented here on GCS â€”
    not a reuse of the torn-down Azure architecture). Returns metadata
    only; the frontend passes it back in AddRegulationRequest.documents
    on the actual POST /regulations call.

    Gated with require_admin_or_sme â€” matches every other Add Regulation
    action on this screen.
    """
    require_admin_or_sme(ctx)

    file_bytes = await file.read()
    try:
        metadata = upload_document(
            tenant_id=ctx.tenant_id,
            file_bytes=file_bytes,
            filename=file.filename,
            content_type=file.content_type or "application/octet-stream",
            actor_uid=ctx.uid,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "file_name": metadata["fileName"],
        "gcs_uri": metadata["gcsUri"],
        "content_type": metadata["contentType"],
        "size_bytes": metadata["sizeBytes"],
        "uploaded_by": metadata["uploadedBy"],
        "uploaded_at": metadata["uploadedAt"].isoformat(),
    }


@app.post("/regulations")
def add_regulation(
    payload: AddRegulationRequest,
    background_tasks: BackgroundTasks,
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Corrected regulation-creation endpoint â€” see AddRegulationRequest's
    docstring for what changed and why. id/createdAt/lastUpdated remain
    always server-generated (FR-0.8, unaffected by this correction).

    Gated with require_admin_or_sme rather than require_bu_write_access:
    FR-0.6 only gives the `user` role status-update/upload/download
    rights on the Dashboard, not regulation creation, so this follows
    the same tenant-configuration-action pattern as POST /users and
    POST /business-units rather than a per-BU scope check.
    """
    require_admin_or_sme(ctx)

    payload_dict = payload.model_dump()
    
        # T009 hardening (NFR-1, NFR-2): document metadata comes from the
    # client, so it is never trusted as-is.
    #  - gcs_uri must sit inside THIS tenant's folder, otherwise a caller
    #    could attach another tenant's file to their own record.
    #  - uploaded_by is always the verified caller, never client-supplied.
    #  - documents: null is normalised to [] (previously a 500).
    tenant_prefix = f"gs://{BUCKET_NAME}/tenants/{ctx.tenant_id}/"
    payload_dict["documents"] = payload_dict.get("documents") or []
    for doc in payload_dict["documents"]:
        uri = doc["gcs_uri"]
        rest = uri[len(tenant_prefix):] if uri.startswith(tenant_prefix) else None
        if not rest or ".." in rest.split("/"):
            raise HTTPException(
                status_code=400,
                detail=f"Document '{doc['file_name']}' does not belong to this tenant.",
            )
        doc["uploaded_by"] = ctx.uid

    errors = validate_regulation_payload(payload_dict)
    if errors:
        raise HTTPException(status_code=400, detail=errors)

    record = create_regulation(
        tenant_id=ctx.tenant_id,
        payload=payload_dict,
        actor_uid=ctx.uid,
        actor_role=ctx.role,
    )
    # T025 (FR-3.1, DL-9): email alert, sent after the response;
    # skipped when the regulation is not on the Roadmap/Dashboard.
    from core.alerts import schedule_regulation_added_alert
    schedule_regulation_added_alert(background_tasks, ctx.tenant_id, record["id"], ctx.uid)
    return record


@app.get("/regulations")
def get_regulations(ctx: RequestContext = Depends(get_current_context)):
    """
    T011 (FR-1.1, FR-1.4): lists regulations in the CALLER's tenant only,
    for the Roadmap (and later the Dashboard, T017). Viewing is tenant-wide
    for every role, so there is no role gate here; only editing is
    BU-scoped (FR-0.2). ctx.tenant_id comes from the verified token, so
    there is no parameter a caller could change to reach another tenant.
    """
    try:
        return [with_document_flags(r) for r in list_regulations(ctx.tenant_id)]
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# T014: edit / remove regulation endpoints (core/regulation_routes.py)
from core.regulation_routes import router as regulation_edit_router
app.include_router(regulation_edit_router)

# T020: saved Dashboard view routes (core/preference_routes.py); GET /regulations
# document flags (core/regulation_routes.py).
from core.regulation_routes import with_document_flags
from core.preference_routes import router as preference_router
app.include_router(preference_router)


# T015 (NFR-4): gzip responses over 1 KB; GET /regulations is ~770 KB at 2,000 records
from fastapi.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=1000)
