"""
core/classify_routes.py - T035 (FR-5.2, M5): POST /regulations/suggest-fields.

Admin/SME (only they add regulations). The SME pastes the regulation's
text on Add Regulation and clicks "Suggest fields"; the answer holds a
suggestion (or "Not sure") for Domain, Country, Region and Business Units,
each with a short reason. Nothing is saved: the form fills a field only when
the SME clicks Use (approved design, Nov 17), and the pasted text is never
stored - only the usual usage record (tokens, cost; never prompt or answer
text) is written by the AI client (feature "classify"), against the
company's daily limit.

OPEN-9: when the AI is unavailable, the limit is reached, or its answer is
unusable, the answer is 200 with aiAvailable false and a plain message, so
the form simply works as it does without the feature.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import ai_client, classify
from core.deps import RequestContext, get_current_context, require_admin_or_sme

logger = logging.getLogger(__name__)
router = APIRouter()

UNAVAILABLE = "Suggestions aren't available right now. Fill in the fields as usual."


class SuggestFieldsRequest(BaseModel):
    text: str = ""


def _units(ctx: RequestContext) -> dict:
    return {d.id: (d.to_dict() or {}).get("label") or d.id for d in ctx.tenant_ref.collection("businessUnits").stream()}


@router.post("/regulations/suggest-fields")
def suggest_fields(payload: SuggestFieldsRequest, ctx: RequestContext = Depends(get_current_context)):
    require_admin_or_sme(ctx)
    try:
        text, cut = classify.prepare_text(payload.text)
    except classify.TextTooShort as e:
        raise HTTPException(status_code=400, detail=str(e))

    units = _units(ctx)
    try:
        answer, _ = ai_client.generate_json(
            ctx.tenant_id, "classify", classify.user_prompt(text, units),
            system=classify.SYSTEM_PROMPT, max_output_tokens=700, actor_uid=ctx.uid,
        )
        suggestions = classify.parse_output(answer, units)
    except (ai_client.AIError, classify.InvalidAnswer) as e:
        logger.warning("Field suggestions unavailable: %s", type(e).__name__)
        message = e.message if isinstance(e, ai_client.AILimitReached) else UNAVAILABLE
        return {"aiAvailable": False, "message": message, "suggestions": None, "textCut": cut}
    return {"aiAvailable": True, "message": "", "suggestions": suggestions, "textCut": cut}
