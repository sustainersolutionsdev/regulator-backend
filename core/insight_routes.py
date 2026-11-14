"""
core/insight_routes.py - POST /insights/summary (T031; FR-4.5, FR-4.6) and
POST /insights/chart-notes (T076: the AI notes under the Insights charts).

The AI summary behind each "View summary" link in the Insights page's
Compliance Overview: one status row (Compliant, Pending, Action needed,
Not set) or "All" (the whole view, FR-4.6), for the page's filters.

Every role, own company only: regulations are read from the caller's own
tenant (ctx.tenant_ref, from the verified token; NFR-1) and the filters are
applied here - the browser's counts are never trusted.

Saved per view and row (tenants/{t}/aiInsightSummaries/{key}) with a
fingerprint of everything the summary was written from. The same view
reopens instantly at no AI cost; any change to a regulation, a status or an
explanation in it regenerates the summary the next time it is opened
(FR-4.7). An empty row needs no AI and costs nothing.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import ai_client, chart_notes, insights
from core.deps import RequestContext, get_current_context
from core.regulation_routes import with_document_flags
from core.summaries import contacts_by_unit

logger = logging.getLogger(__name__)
router = APIRouter()


class InsightSummaryRequest(BaseModel):
    group: str
    filters: dict = {}


class ChartNotesRequest(BaseModel):
    filters: dict = {}


def _regulations(ctx: RequestContext) -> list[dict]:
    out = []
    for d in ctx.tenant_ref.collection("regulations").stream():
        data = d.to_dict() or {}
        if not data.get("hidden"):
            out.append(with_document_flags({"id": d.id, **data}))
    return out


def _present(group, f, kept, more, total, lines, stored, contacts, cached, ai_generated=True):
    by_id = {line["id"]: line["text"] for line in (lines or [])}
    generated = stored.get("generatedAt") if stored else None
    return {
        "group": group,
        "groupLabel": insights.GROUP_LABELS[group],
        "aiGenerated": ai_generated,
        "disclaimer": insights.DISCLAIMER,
        "generatedAt": generated.isoformat() if isinstance(generated, datetime) else generated,
        "model": (stored or {}).get("model", ""),
        "cached": cached,
        "counts": total,
        "short": (stored or {}).get("short", ""),
        "overview": (stored or {}).get("overview", ""),
        "entries": [
            {
                "regulationId": e["regulationId"], "name": e["name"], "title": e["title"], "dueDate": e["dueDate"],
                "unit": e["unit"], "unitCode": e["unitCode"], "status": None if e["status"] == "not_set" else e["status"],
                "statusLabel": e["statusLabel"], "text": by_id.get(e["id"], ""),
                "contacts": contacts.get(e["unitCode"], []) if e["status"] in ("action_needed", "not_set") else [],
            }
            for e in kept
        ],
        "moreCount": len(more),
        "moreNames": [f"{e['name']} ({e['unit']})" for e in more[: insights.MAX_MORE_NAMES]],
    }


@router.post("/insights/summary")
def insight_summary(payload: InsightSummaryRequest, ctx: RequestContext = Depends(get_current_context)):
    if payload.group not in insights.GROUPS:
        raise HTTPException(status_code=400, detail=f"Unknown summary '{payload.group}'.")
    try:
        f = insights.clean_filters(payload.filters)
    except insights.BadFilters as e:
        raise HTTPException(status_code=400, detail=str(e))
    group = payload.group

    unit_names = {d.id: (d.to_dict() or {}).get("label") or d.id for d in ctx.tenant_ref.collection("businessUnits").stream()}
    users = {d.id: (d.to_dict() or {}) for d in ctx.tenant_ref.collection("users").stream()}
    regs = insights.filter_regulations(_regulations(ctx), f)
    entries = insights.build_entries(regs, f, group, unit_names)
    total = insights.counts(entries)
    contacts = contacts_by_unit(users, list(unit_names))

    if not entries:
        label = insights.GROUP_LABELS[group]
        short = ("There are no regulations in this view." if group == "all"
                 else f"No Business Unit statuses are {label} in this view.")
        return _present(group, f, [], [], total, [], {"short": short}, contacts, cached=False, ai_generated=False)

    kept, more = insights.plan_budget(entries)
    model = ai_client.settings()["model"]
    fp = insights.fingerprint(kept, more, group, model)
    cache_ref = ctx.tenant_ref.collection("aiInsightSummaries").document(insights.filters_key(f, group))
    snap = cache_ref.get()
    stored = (snap.to_dict() or {}) if snap.exists else {}
    if stored.get("fingerprint") == fp and stored.get("short"):
        return _present(group, f, kept, more, total, stored.get("entries"), stored, contacts, cached=True)

    try:
        data, result = ai_client.generate_json(
            ctx.tenant_id, f"insights_{group}",
            insights.user_prompt(group, kept, more, total, insights.view_label(f, unit_names)),
            system=insights.SYSTEM_PROMPT, max_output_tokens=3000, actor_uid=ctx.uid,
        )
        parsed = insights.parse_output(data, kept)
    except ai_client.AIError as e:
        raise HTTPException(status_code=e.http_status, detail=e.message)
    except insights.InvalidSummary as e:
        logger.warning("Insights summary (%s) rejected: %s", group, e)
        raise HTTPException(status_code=502, detail=ai_client.AINoAnswer.message)

    stored = {
        "fingerprint": fp, "generatedAt": datetime.now(timezone.utc), "generatedBy": ctx.uid,
        "model": result.model, "promptVersion": insights.PROMPT_VERSION, "group": group, "filters": f,
        "short": parsed["short"], "overview": parsed["overview"], "entries": parsed["entries"],
    }
    try:
        cache_ref.set(stored)
    except Exception as e:  # noqa: BLE001 - the answer is still good; it just isn't reused
        logger.warning("Insights summary cache write failed: %s", type(e).__name__)
    return _present(group, f, kept, more, total, parsed["entries"], stored, contacts, cached=False)


@router.post("/insights/chart-notes")
def insight_chart_notes(payload: ChartNotesRequest, ctx: RequestContext = Depends(get_current_context)):
    """
    T076: a 2-line AI note for each chart in the view, written in one AI call
    from the chart numbers only (no explanations, notes, documents or
    people). Every role, own company only; filters applied here. Saved per
    view with a fingerprint of the numbers: the same view costs nothing
    again, and any change to a count regenerates the notes. An empty view
    makes no AI call.
    """
    try:
        f = insights.clean_filters(payload.filters)
    except insights.BadFilters as e:
        raise HTTPException(status_code=400, detail=str(e))

    unit_names = {d.id: (d.to_dict() or {}).get("label") or d.id for d in ctx.tenant_ref.collection("businessUnits").stream()}
    regs = insights.filter_regulations(_regulations(ctx), f)
    data = chart_notes.chart_data(regs, f, unit_names)
    base = {"aiGenerated": False, "disclaimer": insights.DISCLAIMER, "generatedAt": None, "model": "",
            "cached": False, "notes": {}}
    if not chart_notes.has_data(data):
        return base

    view = insights.view_label(f, unit_names)
    model = ai_client.settings()["model"]
    fp = chart_notes.fingerprint(data, view, model)
    cache_ref = ctx.tenant_ref.collection("aiInsightSummaries").document(insights.filters_key(f, "charts"))
    snap = cache_ref.get()
    stored = (snap.to_dict() or {}) if snap.exists else {}

    def present(s, cached):
        generated = s.get("generatedAt")
        return {**base, "aiGenerated": True, "cached": cached, "model": s.get("model", ""), "notes": s.get("notes", {}),
                "generatedAt": generated.isoformat() if isinstance(generated, datetime) else generated}

    if stored.get("fingerprint") == fp and stored.get("notes"):
        return present(stored, True)

    try:
        answer, result = ai_client.generate_json(
            ctx.tenant_id, "insights_charts", chart_notes.user_prompt(data, view),
            system=chart_notes.SYSTEM_PROMPT, max_output_tokens=1200, actor_uid=ctx.uid,
        )
        notes = chart_notes.parse_output(answer, data, {code: name for code, name in unit_names.items()})
    except ai_client.AIError as e:
        raise HTTPException(status_code=e.http_status, detail=e.message)
    except chart_notes.InvalidNotes as e:
        logger.warning("Chart notes rejected: %s", e)
        raise HTTPException(status_code=502, detail=ai_client.AINoAnswer.message)

    stored = {"fingerprint": fp, "generatedAt": datetime.now(timezone.utc), "generatedBy": ctx.uid,
              "model": result.model, "promptVersion": chart_notes.PROMPT_VERSION, "group": "charts", "filters": f,
              "notes": notes}
    try:
        cache_ref.set(stored)
    except Exception as e:  # noqa: BLE001 - the notes are still good; they just aren't reused
        logger.warning("Chart notes cache write failed: %s", type(e).__name__)
    return present(stored, False)
