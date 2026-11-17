"""
core/duplicate_routes.py - T034 (FR-5.1, M5): the duplicate check on Add
Regulation, and the gate on POST /regulations that makes the SME's
confirmation required, not just shown.

POST /regulations/check-duplicates   Admin/SME (only they add regulations).
    Called by the form when the SME clicks Save, before the regulation is
    created. Returns what the "This may already be in Regulator" box shows.
    Own company only: existing regulations come from the caller's tenant
    (NFR-1). The AI is asked only when the text check finds similar
    regulations (core/duplicates.py), so most saves cost nothing; every AI
    call counts toward the company's daily limit and usage log
    (feature "duplicate_check").

require_duplicate_confirmation(ctx, payload)   used by POST /regulations.
    A clear match (same citation, or the same title / abbreviated name)
    that the SME has not confirmed returns 409 with the matches, so the
    form shows the box even if its own check was skipped. AI-only matches
    are confirmed in the box by the same "Save as a new regulation" choice.
    The confirmation is stored on the new regulation and audited
    (record_duplicate_confirmation), with who confirmed it and when.

OPEN-9 (approved design, Nov 17): the AI never blocks a save. If it is
unavailable, or the daily limit is reached, the text check alone decides
and the answer says so (aiUnavailable).
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import ai_client, duplicates
from core.deps import RequestContext, get_current_context, require_admin_or_sme

logger = logging.getLogger(__name__)
router = APIRouter()

CONFIRM_FIELD = "confirmed_not_duplicate_of"
UNAVAILABLE_NOTE = "Matched by title; the AI check wasn't available."


class DuplicateCheckRequest(BaseModel):
    title: str = ""
    abbreviated_title: str = ""
    due_date: str = ""
    domain: str = ""
    country: str = ""
    region: str = ""
    notes: str = ""
    business_unit_ids: list[str] = []


def _existing(tenant_id: str) -> list[dict]:
    from core.tenancy import list_regulations  # removed regulations are already left out

    return list_regulations(tenant_id)


@router.post("/regulations/check-duplicates")
def check_duplicates(payload: DuplicateCheckRequest, ctx: RequestContext = Depends(get_current_context)):
    require_admin_or_sme(ctx)
    entry = payload.model_dump()
    if not entry["title"].strip() and not entry["abbreviated_title"].strip():
        raise HTTPException(status_code=400, detail="Enter a title to check for duplicates.")

    cands = duplicates.candidates(entry, _existing(ctx.tenant_id))
    base = {"aiChecked": False, "aiUnavailable": False, "note": ""}
    if not cands:
        return {**duplicates.decide(entry, [], None), **base}

    verdicts = None
    try:
        answer, _ = ai_client.generate_json(
            ctx.tenant_id, "duplicate_check", duplicates.user_prompt(entry, cands),
            system=duplicates.SYSTEM_PROMPT, max_output_tokens=800, actor_uid=ctx.uid,
        )
        verdicts = duplicates.parse_output(answer, cands)
        base["aiChecked"] = True
    except (ai_client.AIError, duplicates.InvalidAnswer) as e:
        logger.warning("Duplicate check without AI: %s", type(e).__name__)
        base["aiUnavailable"] = True
    result = {**duplicates.decide(entry, cands, verdicts), **base}
    if base["aiUnavailable"] and result["likelyDuplicates"]:
        result["note"] = UNAVAILABLE_NOTE
    return result


def require_duplicate_confirmation(ctx: RequestContext, payload: dict, confirmed: list | None = None) -> list[str]:
    """
    Refuses the save while a clear match is unconfirmed. `confirmed` is the
    SME's list (main.py takes it out of the payload first, since it is not a
    regulation field; when not given it is taken from the payload here).
    Returns the confirmed ids that exist in this company, to record after
    the save.
    """
    if confirmed is None:
        confirmed = payload.pop(CONFIRM_FIELD, None)
    confirmed = [str(i) for i in (confirmed or []) if str(i).strip()]
    entry = {"title": payload.get("title"), "abbreviated_title": payload.get("abbreviated_title"), "notes": payload.get("notes")}
    regs = _existing(ctx.tenant_id)
    unconfirmed = [m for m in duplicates.exact_matches(entry, regs) if m["reg"]["id"] not in confirmed]
    if unconfirmed:
        result = duplicates.decide(entry, unconfirmed, None)
        raise HTTPException(status_code=409, detail={
            "code": "possible_duplicate",
            "message": "This may already be in Regulator. Confirm it is a different regulation to save it.",
            "likelyDuplicates": result["likelyDuplicates"],
        })
    known = {r["id"] for r in regs}
    return [i for i in dict.fromkeys(confirmed) if i in known]


def record_duplicate_confirmation(ctx: RequestContext, regulation_id: str, confirmed: list[str]) -> None:
    """Stores the SME's "this is a different regulation" choice on the new regulation, audited (NFR-2)."""
    if not confirmed:
        return
    from core.audit import write_with_audit

    ref = ctx.tenant_ref.collection("regulations").document(regulation_id)
    write_with_audit(
        tenant_id=ctx.tenant_id, doc_ref=ref,
        data={"duplicateCheck": {"confirmedNotDuplicateOf": confirmed, "confirmedBy": ctx.uid,
                                 "confirmedAt": datetime.now(timezone.utc)}},
        actor_uid=ctx.uid, actor_role=ctx.role, action="confirm_not_duplicate",
    )
