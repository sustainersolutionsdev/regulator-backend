"""
core/summary_routes.py - GET /regulations/{id}/summary (T029).

Every role, own company only (viewing is company-wide, FR-0.2). Opened from
the summary icon on the Roadmap and the Dashboard; both get the same stored
summary (DL-18 point 5).

  1. Gather the inputs (core/summaries.build_facts) and fingerprint them.
  2. If tenants/{t}/aiSummaries/{id} holds a summary with the same
     fingerprint, return it: no AI call, no cost, identical wording.
  3. Otherwise read the documents (core/doc_reader.py), ask Gemini
     (core/ai_client.py: usage log + daily limit), check the answer
     (core/summaries.parse_output), store it and return it (FR-4.7: a
     change to any input regenerates the summary the next time it is opened).

AI problems come back with the AI client's plain-language message and its
status code (e.g. 429 for the daily limit), which the summary box shows.
"""
import base64
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from core import ai_client, summaries
from core.doc_reader import read_document
from core.deps import RequestContext, get_current_context
from core.regulation_routes import _entries, _get_live_regulation, with_document_flags
from core.storage import BUCKET_NAME, storage_client

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_INLINE_PDF_BYTES = 15 * 1024 * 1024   # all PDFs in one request together
MAX_TEXT_TOTAL = 120_000                   # all extracted text in one request together


def _users(ctx: RequestContext) -> dict:
    return {d.id: (d.to_dict() or {}) for d in ctx.tenant_ref.collection("users").stream()}


def _unit_names(ctx: RequestContext) -> dict:
    return {d.id: (d.to_dict() or {}).get("label") or d.id for d in ctx.tenant_ref.collection("businessUnits").stream()}


def _download(tenant_id: str, gcs_uri: str) -> bytes | None:
    """The file's bytes, only if it sits inside this company's folder (NFR-1)."""
    prefix = f"gs://{BUCKET_NAME}/tenants/{tenant_id}/"
    rest = gcs_uri[len(prefix):] if gcs_uri.startswith(prefix) else None
    if not rest or ".." in rest.split("/"):
        return None
    blob = storage_client().bucket(BUCKET_NAME).blob(f"tenants/{tenant_id}/{rest}")
    try:
        return blob.download_as_bytes()
    except Exception as e:  # noqa: BLE001
        logger.warning("Summary: document download failed (%s)", type(e).__name__)
        return None


def read_documents(tenant_id: str, reg: dict, facts: dict) -> tuple[list[dict], dict]:
    """Reads up to MAX_DOCS documents within the size budgets.
    Returns (reads for the prompt, report {read, partlyRead, notRead})."""
    by_key = {d["key"]: d for d in reg.get("documents") or []}
    reads, report = [], {"read": [], "partlyRead": [], "notRead": []}
    pdf_bytes = text_chars = 0
    for i, doc in enumerate(facts["documents"]):
        name = doc["fileName"]
        if i >= summaries.MAX_DOCS:
            report["notRead"].append({"fileName": name, "reason": f"only the first {summaries.MAX_DOCS} documents are read"})
            continue
        data = _download(tenant_id, by_key.get(doc["key"], {}).get("gcsUri", ""))
        if data is None:
            report["notRead"].append({"fileName": name, "reason": "the file could not be found"})
            continue
        r = read_document(data, name)
        if r.kind == "unreadable":
            report["notRead"].append({"fileName": name, "reason": r.reason})
            continue
        if r.kind == "pdf":
            if pdf_bytes + len(r.data) > MAX_INLINE_PDF_BYTES:
                report["notRead"].append({"fileName": name, "reason": "the summary's size limit was reached"})
                continue
            pdf_bytes += len(r.data)
            reads.append({"fileName": name, "scope": doc["scope"], "kind": "pdf",
                          "data_b64": base64.b64encode(r.data).decode("ascii")})
        else:
            room = MAX_TEXT_TOTAL - text_chars
            if room <= 1000:
                report["notRead"].append({"fileName": name, "reason": "the summary's size limit was reached"})
                continue
            text, partial = (r.text, r.partial) if len(r.text) <= room else (r.text[:room], True)
            text_chars += len(text)
            reads.append({"fileName": name, "scope": doc["scope"], "kind": "text", "text": text, "partial": partial})
        (report["partlyRead"] if reads[-1].get("partial") else report["read"]).append(name)
    return reads, report


@router.get("/regulations/{reg_id}/summary")
def get_regulation_summary(reg_id: str, ctx: RequestContext = Depends(get_current_context)):
    reg_ref, current = _get_live_regulation(ctx, reg_id)
    reg = with_document_flags({"id": reg_id, **current})
    users = _users(ctx)
    people = {uid: {"displayName": u.get("displayName"), "email": u.get("email")} for uid, u in users.items()}
    facts = summaries.build_facts(reg, _entries(reg_ref), _unit_names(ctx), people)
    model = ai_client.settings()["model"]
    fp = summaries.fingerprint(facts, model)
    contacts = summaries.contacts_by_unit(users, [u["code"] for u in facts["units"]])

    cache_ref = ctx.tenant_ref.collection("aiSummaries").document(reg_id)
    snap = cache_ref.get()
    stored = (snap.to_dict() or {}) if snap.exists else {}
    if stored.get("fingerprint") == fp and stored.get("summary"):
        return summaries.present(reg_id, stored, contacts, cached=True)

    reads, report = read_documents(ctx.tenant_id, reg, facts)
    try:
        data, result = ai_client.generate_json(
            ctx.tenant_id, "regulation_summary", summaries.user_parts(facts, reads),
            system=summaries.SYSTEM_PROMPT, max_output_tokens=1500, actor_uid=ctx.uid,
        )
        summary = summaries.parse_output(data, facts, report["notRead"])
    except ai_client.AIError as e:
        raise HTTPException(status_code=e.http_status, detail=e.message)
    except summaries.InvalidSummary as e:
        logger.warning("Summary for %s rejected: %s", reg_id, e)
        raise HTTPException(status_code=502, detail=ai_client.AINoAnswer.message)

    stored = {
        "fingerprint": fp,
        "generatedAt": datetime.now(timezone.utc),
        "generatedBy": ctx.uid,
        "model": result.model,
        "promptVersion": summaries.PROMPT_VERSION,
        "summary": summary,
        "documents": report,
    }
    try:
        cache_ref.set(stored)
    except Exception as e:  # noqa: BLE001 - the answer is still good; it just isn't reused
        logger.warning("Summary cache write failed for %s: %s", reg_id, type(e).__name__)
    return summaries.present(reg_id, stored, contacts, cached=False)
