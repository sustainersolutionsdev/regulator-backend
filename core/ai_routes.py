"""
core/ai_routes.py - AI status and usage (T028, NFR-7).

GET /ai/status  every role. Whether AI is on, and the company's calls today
                against its daily limit. The web app uses it to show or hide
                AI buttons and to explain why they are off.
GET /ai/usage   Admin/SME. Day-by-day AI calls, tokens and estimated cost for
                their own company (last 1-90 days). This is the usage record
                that feeds the pricing question (OPEN-15).

Both read only the caller's own company (ctx.tenant_ref comes from the
verified token; NFR-1). Neither reveals prompts or answers: the usage log
holds counts only.
"""
import datetime as dt

from fastapi import APIRouter, Depends, Query

from core import ai_client
from core.deps import RequestContext, get_current_context, require_admin_or_sme

router = APIRouter()

COST_NOTE = "Estimated from list prices. The amount actually billed is shown in Google AI Studio."


@router.get("/ai/status")
def ai_status(ctx: RequestContext = Depends(get_current_context)):
    s = ai_client.settings()
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    snap = ctx.tenant_ref.collection("aiUsageDaily").document(today).get()
    used = int(((snap.to_dict() or {}) if snap.exists else {}).get("calls", 0))
    return {
        "enabled": bool(s["api_key"]),
        "dailyLimit": s["daily_limit"],
        "usedToday": used,
        "remainingToday": max(s["daily_limit"] - used, 0),
    }


@router.get("/ai/usage")
def ai_usage(days: int = Query(30, ge=1, le=90), ctx: RequestContext = Depends(get_current_context)):
    require_admin_or_sme(ctx)
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = []
    for doc in ctx.tenant_ref.collection("aiUsageDaily").stream():
        d = doc.to_dict() or {}
        date = d.get("date") or doc.id
        if date < start:
            continue
        rows.append({
            "date": date,
            "calls": int(d.get("calls", 0)),
            "inputTokens": int(d.get("inputTokens", 0)),
            "outputTokens": int(d.get("outputTokens", 0)),
            "costUsd": round(float(d.get("costUsd", 0.0)), 4),
        })
    rows.sort(key=lambda r: r["date"], reverse=True)
    totals = {k: sum(r[k] for r in rows) for k in ("calls", "inputTokens", "outputTokens")}
    totals["costUsd"] = round(sum(r["costUsd"] for r in rows), 4)
    return {"model": ai_client.settings()["model"], "days": rows, "totals": totals, "note": COST_NOTE}
