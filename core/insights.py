"""
core/insights.py - the AI summaries on the Insights page (T031; FR-4.5,
FR-4.6; DL-18 point 4, DL-19 point 3). Pure functions: no Firestore or
Gemini, so tests need nothing.

A summary covers one row of the Compliance Overview - Compliant, Pending,
Action needed, Not set - or "All" (the summary of the whole view: FR-4.6),
for the page's current filters.

  filter_regulations  the same filters as the page (utils/regulationFilters
                      in the web app), applied on the server to the company's
                      own regulations: the browser's numbers are never trusted
  build_entries       one entry per regulation per Business Unit in that row
                      (decision 3A), red first, then nearest due date
  plan_budget         the approved length rules (examples doc, Nov 10):
                      Action needed ~80 words, Pending 2 lines (~35),
                      Compliant / Not set 1 line (~20); about 2 pages
                      (MAX_WORDS) in all. Every red entry is kept; the rest
                      are kept by nearest due date until the budget is used,
                      and the remainder are named by title - never dropped
                      silently.
  parse_output        checks the AI's JSON against the entries it was given;
                      status words, names, dates and contacts come from the
                      data, never from the AI.
"""
from __future__ import annotations

import hashlib
import json
import re

from core.summaries import DISCLAIMER, STATUS_WORDS, status_label  # noqa: F401  (one disclaimer, one set of words)

PROMPT_VERSION = "t031-1"

NOT_SET = "__none__"  # same key the web app uses for "Not set" / "Not specified"
GROUPS = ("compliant", "pending", "action_needed", "not_set", "all")
GROUP_LABELS = {"compliant": "Compliant", "pending": "Pending", "action_needed": "Action needed",
                "not_set": "Not set", "all": "All"}

WORDS = {"action_needed": 80, "pending": 35, "not_set": 20, "compliant": 20}
ORDER = {"action_needed": 0, "pending": 1, "not_set": 2, "compliant": 3}
MAX_WORDS = 900
MAX_RED = 40            # a hard stop on request size, even for red entries
MAX_MORE_NAMES = 50
MAX_TEXT = 500          # characters per explanation / note sent to the AI

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MULTI = ("domain", "bu", "country", "region")


class BadFilters(ValueError):
    pass


def clean_filters(raw: dict | None) -> dict:
    """The page's filters, checked and normalised (status is not a filter here)."""
    raw = raw or {}
    out = {"q": "", "domain": [], "bu": [], "country": [], "region": [], "dueFrom": "", "dueTo": "", "docs": ""}
    q = raw.get("q") or ""
    if not isinstance(q, str) or len(q) > 200:
        raise BadFilters("search text is too long")
    out["q"] = q.strip()
    for key in MULTI:
        values = raw.get(key) or []
        if not isinstance(values, list) or len(values) > 100 or not all(isinstance(v, str) and len(v) <= 200 for v in values):
            raise BadFilters(f"'{key}' must be a list of short texts")
        out[key] = sorted({v.strip() for v in values if v.strip()})
    for key in ("dueFrom", "dueTo"):
        v = raw.get(key) or ""
        if v and (not isinstance(v, str) or not DATE_RE.match(v)):
            raise BadFilters(f"'{key}' must be YYYY-MM-DD")
        out[key] = v
    docs = raw.get("docs") or ""
    out["docs"] = docs if docs in ("with", "without") else ""
    return out


def _norm(v) -> str:
    return str(v if v is not None else "").strip()


def _match_multi(selected: list, value) -> bool:
    if not selected:
        return True
    v = _norm(value)
    return (v or NOT_SET) in selected


def filter_regulations(regs: list[dict], f: dict) -> list[dict]:
    """regs: the company's regulations with document flags (with_document_flags)."""
    q = f["q"].lower()
    out = []
    for r in regs:
        if r.get("hidden") or r.get("addToRoadmapDashboard") is not True:
            continue
        if q:
            notes = [((r.get("buStatuses") or {}).get(bu) or {}).get("note") for bu in (r.get("buStatuses") or {})]
            hay = " ".join(_norm(x) for x in [r.get("title"), r.get("abbreviatedTitle"), r.get("notes"), *notes]).lower()
            if q not in hay:
                continue
        if not (_match_multi(f["domain"], r.get("domain")) and _match_multi(f["country"], r.get("country"))
                and _match_multi(f["region"], r.get("region"))):
            continue
        if f["bu"] and not any(code in (r.get("businessUnitIds") or []) for code in f["bu"]):
            continue
        due = _norm(r.get("dueDate"))
        if f["dueFrom"] and (not due or due < f["dueFrom"]):
            continue
        if f["dueTo"] and (not due or due > f["dueTo"]):
            continue
        has_docs = any(not d.get("hidden") for d in r.get("documents") or [])
        if (f["docs"] == "with" and not has_docs) or (f["docs"] == "without" and has_docs):
            continue
        out.append(r)
    return out


def _status_key(reg: dict, bu: str) -> str:
    s = ((reg.get("buStatuses") or {}).get(bu) or {}).get("status")
    return s if s in STATUS_WORDS else "not_set"


def build_entries(regs: list[dict], f: dict, group: str, unit_names: dict) -> list[dict]:
    entries = []
    for r in regs:
        units = [bu for bu in (r.get("businessUnitIds") or []) if not f["bu"] or bu in f["bu"]]
        for bu in units:
            key = _status_key(r, bu)
            if group != "all" and key != group:
                continue
            cur = (r.get("buStatuses") or {}).get(bu) or {}
            entries.append({
                "regulationId": r.get("id"),
                "title": r.get("title") or "",
                "name": (r.get("abbreviatedTitle") or "").strip() or r.get("title") or "",
                "dueDate": r.get("dueDate") or "",
                "domain": r.get("domain") or "",
                "unitCode": bu,
                "unit": unit_names.get(bu) or bu,
                "status": key,
                "statusLabel": status_label(None if key == "not_set" else key),
                "explanation": _norm(cur.get("note"))[:MAX_TEXT],
                "smeNotes": _norm(r.get("notes"))[:MAX_TEXT],
            })
    entries.sort(key=lambda e: (ORDER[e["status"]], e["dueDate"] or "9999", e["name"].lower(), e["unit"].lower()))
    return entries


def plan_budget(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """(entries the AI writes about, entries only named by title)."""
    kept, more, used = [], [], 0
    reds = [e for e in entries if e["status"] == "action_needed"]
    for e in reds[:MAX_RED]:
        kept.append(e)
        used += WORDS[e["status"]]
    more.extend(reds[MAX_RED:])
    for e in entries:
        if e["status"] == "action_needed":
            continue
        cost = WORDS[e["status"]]
        if used + cost <= MAX_WORDS:
            kept.append(e)
            used += cost
        else:
            more.append(e)
    position = {id(e): i for i, e in enumerate(entries)}
    kept.sort(key=lambda e: position[id(e)])
    for i, e in enumerate(kept, start=1):
        e["id"] = f"e{i}"
    return kept, more


def counts(entries: list[dict]) -> dict:
    return {"statuses": len(entries), "regulations": len({e["regulationId"] for e in entries})}


def filters_key(f: dict, group: str) -> str:
    """Which saved summary this view and row use (one per view and row)."""
    raw = json.dumps({"f": f, "g": group}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def fingerprint(kept: list[dict], more: list[dict], group: str, model: str) -> str:
    fields = ("regulationId", "name", "dueDate", "unitCode", "unit", "status", "explanation", "smeNotes")
    raw = json.dumps({"v": PROMPT_VERSION, "model": model, "group": group,
                      "kept": [{k: e[k] for k in fields} for e in kept],
                      "more": [[e["regulationId"], e["unitCode"], e["status"], e["name"]] for e in more]},
                     sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


SYSTEM_PROMPT = """You write compliance summaries for Regulator, a tool that company sustainability teams use to track regulations.
You are summarising one group of Business Unit statuses for a busy executive. Plain English, short sentences, no jargon, no legal advice.

Rules:
1. Use ONLY the information provided. Do not use outside knowledge.
2. Never invent dates, citations, requirements, numbers, people or contact details. Never mention email addresses.
3. Never set, change, suggest or predict a compliance status. Statuses are set by people and are given to you.
4. Whenever you mention a status colour, follow it with its status name in brackets: red (Action needed), amber (Pending), green (Compliant). No status is "Not set".
5. Refer to Business Units by their name, never by their code.
6. If something is missing, say so plainly (for example "No explanation has been entered."). Do not fill gaps or suggest actions of your own.

Return JSON only, with exactly these keys:
- "short": at most 3 short sentences (about 60 words): how many statuses the group holds and the most important points, red (Action needed) first.
- "overview": 1 to 3 sentences on the group as a whole: common themes, nearest due dates.
- "entries": one object per entry given, same order: {"id": the entry's id, "text": the entry's line}. Keep each line to the entry's "words" limit.
  For red (Action needed) entries: why it is red and the action being taken, both only from its explanation and notes; if no action is entered, say "No action has been entered yet."
  For other entries: its status explanation in one or two short lines."""


def user_prompt(group: str, kept: list[dict], more: list[dict], all_counts: dict, view_label: str) -> str:
    data = {
        "group": GROUP_LABELS[group],
        "view": view_label,
        "totals": all_counts,
        "entries": [
            {"id": e["id"], "regulation": e["name"], "fullTitle": e["title"], "due": e["dueDate"], "domain": e["domain"],
             "businessUnit": e["unit"], "status": e["statusLabel"], "words": WORDS[e["status"]],
             "explanation": e["explanation"] or "(none entered)", "smeNotes": e["smeNotes"] or "(none entered)"}
            for e in kept
        ],
        "notCoveredInDetail": len(more),
    }
    return "Summarise this group.\n\nDATA:\n" + json.dumps(data, indent=1)


def _text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


class InvalidSummary(ValueError):
    pass


def parse_output(data, kept: list[dict]) -> dict:
    if not isinstance(data, dict):
        raise InvalidSummary("answer is not an object")
    short = _text(data.get("short"), 500)
    if not short:
        raise InvalidSummary("short missing")
    overview = _text(data.get("overview"), 1200)
    by_id = {}
    for item in data.get("entries") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            by_id.setdefault(item["id"], item.get("text"))
    lines = []
    for e in kept:
        limit = WORDS[e["status"]] * 9
        text = _text(by_id.get(e["id"]), limit) or (e["explanation"][:limit] if e["explanation"] else "No explanation has been entered.")
        lines.append({"id": e["id"], "text": text})
    return {"short": short, "overview": overview, "entries": lines}


def view_label(f: dict, unit_names: dict | None = None) -> str:
    """The filters in words, for the AI (Business Units by name, never by code)."""
    unit_names = unit_names or {}
    parts = []
    if f["dueFrom"] or f["dueTo"]:
        parts.append(f"due {f['dueFrom'] or 'any time'} to {f['dueTo'] or 'any time'}")
    for key, name in (("domain", "domain"), ("bu", "Business Unit"), ("country", "country"), ("region", "region")):
        if f[key]:
            shown = ["Not specified" if v == NOT_SET else (unit_names.get(v, v) if key == "bu" else v) for v in f[key]]
            parts.append(f"{name}: {', '.join(shown)}")
    if f["q"]:
        parts.append(f'search "{f["q"]}"')
    if f["docs"]:
        parts.append(f"{f['docs']} documents")
    return "; ".join(parts) or "all regulations"
