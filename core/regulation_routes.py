"""
Regulation edit routes.

T014 - Edit and remove a regulation (FR-1.4, NFR-2, NFR-8).
T020 - Per-Business-Unit compliance status, Dashboard documents
       (FR-2.1, FR-2.3, FR-2.5 as changed by DL-8).
T022 - Downloads are served with the type that matches the file's
       extension, plus X-Content-Type-Options: nosniff (M2 QC).

Decided by Srinivas, Oct 13 2026 (DL-6):
  1. Main details (title, abbreviated title, due date, domain, country,
     region, business units) can be changed by Admin and SME only.
  2. Users change nothing from the Roadmap; their updates happen on the
     Dashboard only.
  3. Only Admin and SME can remove a regulation.

Decided by Srinivas, Oct 16 2026 (DL-8):
  - Status and its explanation are kept PER BUSINESS UNIT on each
    regulation (`buStatuses`), never as one combined colour.
  - A User updates the status and explanation of their own Business
    Unit(s) only, and cannot edit the SME's main Notes.
  - Users may add supporting documents for their own Business Unit(s) but
    not remove them; hiding a document is Admin/SME only and soft.

So: PATCH /regulations/{id} stays Admin/SME-only and no longer accepts a
status (T018's single status is retired). Status goes through
PATCH /regulations/{id}/bu-status/{bu_code}, gated by
require_bu_write_access(), which is also the real Business Unit-scoped write
that replaces the old /test-write endpoint.

Stored shape (Firestore, camelCase like every other field):
  buStatuses: {
    "<BU code>": {status, note, updatedAt, updatedBy, statusChangedAt}
  }
  documents: [ {fileName, gcsUri, contentType, sizeBytes, uploadedBy,
                uploadedAt, buCode?} ]
  hiddenDocuments: { "<document key>": {hiddenAt, hiddenBy} }

status is "compliant" | "pending" | "action_needed", or null for "not set".
It is always set by a person (FR-2.1: never inferred or AI-generated).

Documents are hidden through a separate map, keyed by a hash of the file's
URI, instead of rewriting the documents array. Each write then touches only
its own key, so an upload and a hide happening at the same moment cannot
overwrite each other (write_with_audit uses set(merge=True), which merges
maps but replaces arrays). with_document_flags() adds `key` and `hidden` to
each document for API responses.

Every write goes through write_with_audit(), so each change lands in the
audit log in the same transaction (NFR-2). Nothing is ever erased (NFR-8).
"""
import hashlib
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from google.cloud import firestore
from pydantic import BaseModel

from core.activity import build_activity, latest_note, make_entry
from core.audit import write_with_audit
from core.deps import RequestContext, get_current_context, require_admin_or_sme, require_bu_write_access
from core.storage import BUCKET_NAME, content_type_for, storage_client, upload_document

router = APIRouter()

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# DL-8: the three person-set statuses. None (null) means "not set".
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
}
REQUIRED_NOT_BLANK = {"title", "due_date", "domain", "business_unit_ids"}


# --- Shared helpers ------------------------------------------------------------

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


def _require_bu_on_regulation(bu_code: str, reg: dict) -> None:
    if bu_code not in (reg.get("businessUnitIds") or []):
        raise HTTPException(
            status_code=400,
            detail=f"This regulation is not tagged to Business Unit '{bu_code}'.",
        )


def document_key(gcs_uri: str) -> str:
    """Stable, Firestore-safe identifier for one document (no '/' or '.')."""
    return hashlib.sha1(gcs_uri.encode("utf-8")).hexdigest()[:16]


def with_document_flags(reg: dict) -> dict:
    """
    Returns the regulation with `key` and `hidden` added to each document,
    for API responses. GET /regulations (main.py) and every endpoint here
    use it, so the frontend never computes document keys itself.
    """
    hidden_map = reg.get("hiddenDocuments") or {}
    docs = []
    for d in reg.get("documents") or []:
        key = document_key(d.get("gcsUri", ""))
        docs.append({**d, "key": key, "hidden": key in hidden_map})
    return {**reg, "documents": docs}


# --- T014: edit and remove (Admin/SME) -------------------------------------------

class UpdateRegulationRequest(BaseModel):
    """
    Every field optional: only the fields sent are changed.
    T020: status is no longer accepted here (DL-8) - it is per Business
    Unit, through PATCH /regulations/{id}/bu-status/{bu_code}. A status sent
    here is ignored like any unknown field.
    """
    title: Optional[str] = None
    abbreviated_title: Optional[str] = None
    due_date: Optional[str] = None
    domain: Optional[str] = None
    country: Optional[str] = None
    region: Optional[str] = None
    business_unit_ids: Optional[list[str]] = None
    notes: Optional[str] = None
    add_to_roadmap_dashboard: Optional[bool] = None


@router.patch("/regulations/{reg_id}")
def update_regulation(
    reg_id: str,
    payload: UpdateRegulationRequest,
    background_tasks: BackgroundTasks,
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
    if errors:
        raise HTTPException(status_code=400, detail=errors)

    update = {}
    for api_field, value in changes.items():
        if value is None:
            # Optional text fields sent as null are cleared to "".
            value = False if api_field == "add_to_roadmap_dashboard" else ""
        if isinstance(value, str):
            value = value.strip()
        update[EDITABLE_FIELDS[api_field]] = value
    update["lastUpdated"] = datetime.now(timezone.utc)
    update["lastUpdatedBy"] = ctx.uid

    write_with_audit(
        tenant_id=ctx.tenant_id,
        doc_ref=reg_ref,
        data=update,
        actor_uid=ctx.uid,
        actor_role=ctx.role,
        action="update_regulation",
    )
    # T026 (FR-3.2, DL-9): email alert after the response: main-detail changes only;
    # "Add to Roadmap and Dashboard" No -> Yes sends the "New regulation" email.
    from core.alerts import schedule_regulation_updated_alert
    schedule_regulation_updated_alert(background_tasks, ctx.tenant_id, reg_id, ctx.uid, current, update)
    return with_document_flags({"id": reg_id, **current, **update})


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


# --- T020: per-Business-Unit status (DL-8) ---------------------------------------

class BuStatusRequest(BaseModel):
    """
    Send status, note, documents, or any mix. status null = "not set".
    document_keys (T065): documents already uploaded for this unit
    (POST /regulations/{id}/documents with bu_code), saved with this update
    as one activity entry.
    """
    status: Optional[str] = None
    note: Optional[str] = None
    document_keys: Optional[list[str]] = None


@router.patch("/regulations/{reg_id}/bu-status/{bu_code}")
def update_bu_status(
    reg_id: str,
    bu_code: str,
    payload: BuStatusRequest,
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Sets one Business Unit's status and/or explanation on one regulation.
    Admin/SME: any Business Unit. User: their own Business Unit(s) only,
    otherwise 403 "You are not permitted to edit Business Unit 'X'." which
    the Dashboard shows on screen (FR-0.2, FR-2.3).
    """
    reg_ref, current = _get_live_regulation(ctx, reg_id)
    require_bu_write_access(bu_code, ctx)
    _require_bu_on_regulation(bu_code, current)

    changes = payload.model_dump(exclude_unset=True)
    doc_keys = [k for k in (changes.pop("document_keys", None) or []) if k]
    if not changes and not doc_keys:
        raise HTTPException(status_code=400, detail="Send a status, a note, or a document.")

    errors = []
    if "status" in changes and changes["status"] is not None and changes["status"] not in STATUS_VALUES:
        errors.append("'status' must be one of: compliant, pending, action_needed (or null for not set)")
    note = (changes.get("note") or "").strip() if "note" in changes else None
    if note is not None and len(note) > STATUS_NOTE_MAX:
        errors.append(f"'note' must be {STATUS_NOTE_MAX} characters or fewer")
    unit_docs = {
        d["key"]: d for d in with_document_flags(current)["documents"]
        if d.get("buCode") == bu_code and not d["hidden"]
    }
    for key in doc_keys:
        if key not in unit_docs:
            errors.append(f"Document '{key}' is not one of this Business Unit's documents")
    if errors:
        raise HTTPException(status_code=400, detail=errors)

    now = datetime.now(timezone.utc)
    previous = (current.get("buStatuses") or {}).get(bu_code) or {}
    entry = {"updatedAt": now, "updatedBy": ctx.uid}
    if "status" in changes:
        entry["status"] = changes["status"]
        if changes["status"] != previous.get("status"):
            entry["statusChangedAt"] = now
    if note is not None:
        entry["note"] = note

    # Nested map + set(merge=True): only this Business Unit's fields change,
    # so two people updating different units at once never overwrite each other.
    data = {"buStatuses": {bu_code: entry}, "lastUpdated": now, "lastUpdatedBy": ctx.uid}

    # T065: one activity entry per save (status change, note, documents),
    # written in the same transaction as the status and its audit entry.
    activity = make_entry(
        bu_code=bu_code, actor_uid=ctx.uid, actor_role=ctx.role, now=now,
        previous_status=previous.get("status"), status_sent="status" in changes,
        new_status=changes.get("status"), note=note, documents=[unit_docs[k] for k in dict.fromkeys(doc_keys)],
    )
    also_set = []
    if activity is not None:
        activity_ref = reg_ref.collection("activity").document()
        also_set.append((activity_ref, activity))
    write_with_audit(
        tenant_id=ctx.tenant_id,
        doc_ref=reg_ref,
        data=data,
        actor_uid=ctx.uid,
        actor_role=ctx.role,
        action="update_bu_status",
        audit_after={**data, "activity": activity} if activity is not None else None,
        also_set=also_set,
    )

    merged = dict(current.get("buStatuses") or {})
    merged[bu_code] = {**previous, **entry}
    return with_document_flags(
        {"id": reg_id, **current, "buStatuses": merged, "lastUpdated": now, "lastUpdatedBy": ctx.uid}
    )


# --- T020: documents on the Dashboard ----------------------------------------------

@router.post("/regulations/{reg_id}/documents")
async def add_regulation_document(
    reg_id: str,
    bu_code: Optional[str] = Form(None),
    file: UploadFile = File(...),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Adds one supporting document to a regulation, for one of its Business
    Units. Users: their own Business Unit(s) only (DL-8). The document is
    tagged with that Business Unit and the uploader; allowed types and the
    25 MB limit are the same as Add Regulation (core/storage.py).

    Without bu_code (Srinivas, Oct 24, item 6: upload from the Roadmap Edit
    panel): a regulation-wide document, like those attached on Add
    Regulation. Admin/SME only, since it applies to every unit.
    """
    reg_ref, current = _get_live_regulation(ctx, reg_id)
    bu_code = (bu_code or "").strip() or None
    if bu_code is None:
        require_admin_or_sme(ctx)
    else:
        require_bu_write_access(bu_code, ctx)
        _require_bu_on_regulation(bu_code, current)

    file_bytes = await file.read()
    try:
        meta = upload_document(
            tenant_id=ctx.tenant_id,
            file_bytes=file_bytes,
            filename=file.filename,
            content_type=file.content_type or "application/octet-stream",
            actor_uid=ctx.uid,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    entry = {**meta, "buCode": bu_code} if bu_code else {**meta}
    now = datetime.now(timezone.utc)
    write_with_audit(
        tenant_id=ctx.tenant_id,
        doc_ref=reg_ref,
        # ArrayUnion appends without rewriting the array, so concurrent
        # uploads to the same regulation cannot drop each other.
        data={"documents": firestore.ArrayUnion([entry]), "lastUpdated": now, "lastUpdatedBy": ctx.uid},
        actor_uid=ctx.uid,
        actor_role=ctx.role,
        action="add_regulation_document",
        audit_after={"documentAdded": entry, "lastUpdated": now, "lastUpdatedBy": ctx.uid},
    )
    return {**entry, "key": document_key(entry["gcsUri"]), "hidden": False}


class HideDocumentRequest(BaseModel):
    key: str


@router.post("/regulations/{reg_id}/documents/hide")
def hide_regulation_document(
    reg_id: str,
    payload: HideDocumentRequest,
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Admin/SME only (DL-8). Soft: the document and its file are kept, and it
    stops appearing on screens and in downloads (NFR-8).
    """
    require_admin_or_sme(ctx)
    reg_ref, current = _get_live_regulation(ctx, reg_id)

    docs = with_document_flags(current)["documents"]
    target = next((d for d in docs if d["key"] == payload.key), None)
    if target is None or target["hidden"]:
        raise HTTPException(status_code=404, detail="Document not found.")

    now = datetime.now(timezone.utc)
    write_with_audit(
        tenant_id=ctx.tenant_id,
        doc_ref=reg_ref,
        data={
            "hiddenDocuments": {payload.key: {"hiddenAt": now, "hiddenBy": ctx.uid, "gcsUri": target["gcsUri"]}},
            "lastUpdated": now,
            "lastUpdatedBy": ctx.uid,
        },
        actor_uid=ctx.uid,
        actor_role=ctx.role,
        action="hide_regulation_document",
    )
    return {"key": payload.key, "hidden": True}


@router.get("/regulations/{reg_id}/documents/{key}")
def download_regulation_document(
    reg_id: str,
    key: str,
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Downloads one supporting document. Every role can download (viewing is
    tenant-wide, iWant item 4). The file must be listed on a regulation in
    the caller's own tenant, not hidden, and stored inside that tenant's
    folder - so no request can reach another tenant's file (NFR-1).
    """
    _, current = _get_live_regulation(ctx, reg_id)
    docs = with_document_flags(current)["documents"]
    target = next((d for d in docs if d["key"] == key and not d["hidden"]), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Document not found.")

    prefix = f"gs://{BUCKET_NAME}/tenants/{ctx.tenant_id}/"
    uri = target.get("gcsUri", "")
    rest = uri[len(prefix):] if uri.startswith(prefix) else None
    if not rest or ".." in rest.split("/"):
        raise HTTPException(status_code=404, detail="Document not found.")

    blob = storage_client().bucket(BUCKET_NAME).blob(f"tenants/{ctx.tenant_id}/{rest}")
    if not blob.exists():
        raise HTTPException(status_code=404, detail="Document not found.")
    content = blob.download_as_bytes()

    filename = target.get("fileName") or "document"
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "")
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    # T022: the type comes from the file's extension, never from the stored
    # contentType (on records made through Add Regulation that was whatever
    # the browser sent), and the browser is told not to guess.
    return Response(
        content=content,
        media_type=content_type_for(filename),
        headers={"Content-Disposition": disposition, "X-Content-Type-Options": "nosniff"},
    )


# --- T065: Business Unit activity (Srinivas, Oct 29) --------------------------------

def _people(ctx: RequestContext) -> dict:
    """uid -> {displayName, email} for the caller's company (names in the history)."""
    return {
        d.id: {"displayName": (d.to_dict() or {}).get("displayName"), "email": (d.to_dict() or {}).get("email")}
        for d in ctx.tenant_ref.collection("users").stream()
    }


def _entries(reg_ref, bu_code: Optional[str] = None) -> list[dict]:
    query = reg_ref.collection("activity")
    if bu_code is not None:
        query = query.where("buCode", "==", bu_code)  # single-field filter: no composite index needed
    return [{"id": d.id, **(d.to_dict() or {})} for d in query.stream()]


@router.get("/regulations/{reg_id}/activity")
def get_regulation_activity(
    reg_id: str,
    bu_code: Optional[str] = None,
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Who changed what on each Business Unit, newest first (T065). Every role
    sees every unit's history (viewing is company-wide, FR-0.2), so people
    know whom to contact. Each entry carries the person's name and email,
    its documents (hidden ones left out) and whether the caller may remove it.
    """
    reg_ref, current = _get_live_regulation(ctx, reg_id)
    if bu_code is not None:
        _require_bu_on_regulation(bu_code, current)
    reg = with_document_flags(current)
    return build_activity(reg, _entries(reg_ref, bu_code), _people(ctx), ctx.uid, bu_code)


@router.delete("/regulations/{reg_id}/activity/{entry_id}")
def remove_activity_note(
    reg_id: str,
    entry_id: str,
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Removes your own note (T065). Soft: the entry is kept and marked removed
    (NFR-8), and stops showing. An entry that changed the status or added a
    document can't be removed. If it was the unit's latest note, the unit
    shows the previous one.
    """
    reg_ref, current = _get_live_regulation(ctx, reg_id)
    entry_ref = reg_ref.collection("activity").document(entry_id)
    snap = entry_ref.get()
    entry = {"id": entry_id, **(snap.to_dict() or {})} if snap.exists else None
    if entry is None or entry.get("removed"):
        raise HTTPException(status_code=404, detail="Note not found.")
    if entry.get("actorUid") != ctx.uid:
        raise HTTPException(status_code=403, detail="You can only remove your own notes.")
    if entry.get("statusChanged") or entry.get("documents") or not entry.get("note"):
        raise HTTPException(
            status_code=400,
            detail="Only a note on its own can be removed; a status change or document stays on record.",
        )

    bu_code = entry.get("buCode")
    now = datetime.now(timezone.utc)
    also_set = []
    unit = (current.get("buStatuses") or {}).get(bu_code) or {}
    if (unit.get("note") or "").strip() == entry["note"]:
        replacement = latest_note(_entries(reg_ref, bu_code), bu_code, skip_id=entry_id)
        also_set.append((reg_ref, {"buStatuses": {bu_code: {"note": replacement}}}))

    write_with_audit(
        tenant_id=ctx.tenant_id,
        doc_ref=entry_ref,
        data={"removed": True, "removedAt": now, "removedBy": ctx.uid},
        actor_uid=ctx.uid,
        actor_role=ctx.role,
        action="remove_activity_note",
        also_set=also_set,
    )
    return {"id": entry_id, "removed": True}
