"""
core/chart_notes.py - AI notes on the Insights charts (T076, second drop;
DL-20 point 4; Nov 9 decision 14: "a 2-line note on a chart"). Pure
functions: no Firestore or Gemini, so tests need nothing.

One AI call writes the notes for every chart in the current view at once
(cheaper than one call per chart), and the result is saved by a fingerprint
of the chart numbers, so the same view costs nothing the second time.

  chart_data     the numbers behind each chart, counted on the server with
                 the same rules as the page (utils/insights.js): regulations
                 once; statuses once per Business Unit (decision 3A); with a
                 Business Unit filter only the selected units count.
  user_prompt    those numbers, and nothing else: the AI never sees
                 explanations, notes, documents or people for these notes.
  parse_output   keeps a note only for a chart that has data, cut to two
                 short sentences; email addresses and unit codes removed
                 by code (scrub, T033).

The charts:
  domain, region, country   regulations by Domain / Region / Country
  bu                        Business Unit statuses by Business Unit
  quarter (T077)            Business Unit statuses by due-date quarter, split by status
  grid (T077)               Business Unit x status
"""
from __future__ import annotations

import hashlib
import json

from core.insights import NOT_SET, STATUS_WORDS, _norm, _status_key, _text, scrub

PROMPT_VERSION = "t076-1"

CHARTS = ("domain", "region", "country", "bu", "quarter", "grid")
CHART_TITLES = {
    "domain": "Regulations by domain",
    "region": "Regulations by region",
    "country": "Regulations by country",
    "bu": "Regulations by Business Unit",
    "quarter": "Due dates by quarter, by status",
    "grid": "Business Unit by status",
}
STATUS_KEYS = ("compliant", "pending", "action_needed", "not_set")
STATUS_NAMES = {"compliant": "Compliant", "pending": "Pending", "action_needed": "Action needed", "not_set": "Not set"}
NOT_SPECIFIED = "Not specified"
NO_DUE_DATE = "No due date"
MAX_ROWS = 25      # rows per chart sent to the AI; the rest are summed as "Other"
NOTE_LIMIT = 260   # characters per note: two short sentences


def _tally(values: list[str]) -> list[dict]:
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return [{"label": k, "count": n} for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _field(reg: dict, name: str) -> str:
    return _norm(reg.get(name)) or NOT_SPECIFIED


def _units_in_scope(reg: dict, bu_filter: list[str]) -> list[str]:
    units = reg.get("businessUnitIds") or []
    return [u for u in units if u in bu_filter] if bu_filter else list(units)


def quarter_of(due: str) -> str:
    """'2026-10-21' -> '2026 Q4' (sorts by time); no date -> 'No due date'."""
    due = _norm(due)
    if len(due) < 7 or not due[:4].isdigit() or not due[5:7].isdigit():
        return NO_DUE_DATE
    return f"{due[:4]} Q{(int(due[5:7]) - 1) // 3 + 1}"


def chart_data(regs: list[dict], f: dict, unit_names: dict) -> dict:
    """regs: already filtered (insights.filter_regulations)."""
    unit_rows = []  # (unit name, status key, quarter)
    for r in regs:
        q = quarter_of(r.get("dueDate"))
        for code in _units_in_scope(r, f.get("bu") or []):
            unit_rows.append((unit_names.get(code, code), _status_key(r, code), q))

    quarters: dict[str, dict] = {}
    grid: dict[str, dict] = {}
    for name, status, q in unit_rows:
        quarters.setdefault(q, {k: 0 for k in STATUS_KEYS})[status] += 1
        grid.setdefault(name, {k: 0 for k in STATUS_KEYS})[status] += 1

    return {
        "totals": {"regulations": len(regs), "businessUnitStatuses": len(unit_rows)},
        "domain": _tally([_field(r, "domain") for r in regs]),
        "region": _tally([_field(r, "region") for r in regs]),
        "country": _tally([_field(r, "country") for r in regs]),
        "bu": _tally([name for name, _, _ in unit_rows]),
        "quarter": [{"label": q, **quarters[q]} for q in sorted(quarters, key=lambda x: (x == NO_DUE_DATE, x))],
        "grid": [{"label": n, **grid[n]} for n in sorted(grid, key=lambda n: (-sum(grid[n].values()), n))],
    }


def has_data(data: dict) -> bool:
    return data["totals"]["regulations"] > 0


def _trim(rows: list[dict], status_rows: bool = False) -> list[dict]:
    if len(rows) <= MAX_ROWS:
        return rows
    head, rest = rows[: MAX_ROWS - 1], rows[MAX_ROWS - 1:]
    if status_rows:
        other = {"label": f"Other ({len(rest)})", **{k: sum(r[k] for r in rest) for k in STATUS_KEYS}}
    else:
        other = {"label": f"Other ({len(rest)})", "count": sum(r["count"] for r in rest)}
    return head + [other]


def fingerprint(data: dict, view: str, model: str) -> str:
    raw = json.dumps({"v": PROMPT_VERSION, "model": model, "view": view, "data": data}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


SYSTEM_PROMPT = """You write short notes under charts in Regulator, a tool that company sustainability teams use to track regulations.
Each note helps a busy executive read one chart at a glance. Plain English, no jargon, no legal advice.

Rules:
1. Use ONLY the numbers provided. Do not use outside knowledge and do not explain why the numbers are what they are.
2. Never invent numbers, names, dates or trends. Percentages are allowed only when worked out from the numbers given.
3. Never suggest, predict or recommend a compliance status or an action.
4. When you mention a status, use its name: Compliant, Pending, Action needed or Not set. You may add the colour in front, as in "red (Action needed)".
5. Refer to Business Units by the names given.
6. Each note is at most 2 short sentences (about 35 words): the largest item or the main pattern first, then one useful contrast (for example the next largest, or where Action needed is concentrated). Do not just list every item.
7. Regulations are counted once. Business Unit statuses count a regulation once per Business Unit, so their total can be higher than the number of regulations. Say "Business Unit statuses" for those charts.

Return JSON only: an object with one key per chart given ("domain", "region", "country", "bu", "quarter", "grid"), each a string."""


def user_prompt(data: dict, view: str) -> str:
    charts = {
        "domain": {"title": CHART_TITLES["domain"], "counts": "regulations", "rows": _trim(data["domain"])},
        "region": {"title": CHART_TITLES["region"], "counts": "regulations", "rows": _trim(data["region"])},
        "country": {"title": CHART_TITLES["country"], "counts": "regulations", "rows": _trim(data["country"])},
        "bu": {"title": CHART_TITLES["bu"], "counts": "Business Unit statuses", "rows": _trim(data["bu"])},
        "quarter": {"title": CHART_TITLES["quarter"], "counts": "Business Unit statuses per due-date quarter, by status",
                    "rows": _rename(_trim(data["quarter"], True))},
        "grid": {"title": CHART_TITLES["grid"], "counts": "Business Unit statuses per Business Unit, by status",
                 "rows": _rename(_trim(data["grid"], True))},
    }
    payload = {"view": view, "totals": data["totals"], "charts": {k: v for k, v in charts.items() if v["rows"]}}
    return "Write one note per chart.\n\nDATA:\n" + json.dumps(payload, indent=1)


def _rename(rows: list[dict]) -> list[dict]:
    return [{"label": r["label"], **{STATUS_NAMES[k]: r[k] for k in STATUS_KEYS}} for r in rows]


class InvalidNotes(ValueError):
    pass


def parse_output(answer, data: dict, unit_codes: dict) -> dict:
    """{chart: note} for every chart that has rows; the AI's text, cleaned."""
    if not isinstance(answer, dict):
        raise InvalidNotes("answer is not an object")
    notes = {}
    for key in CHARTS:
        if not data.get(key):
            continue
        text = scrub(_text(answer.get(key), NOTE_LIMIT * 2), unit_codes)
        if not text:
            continue
        notes[key] = _two_sentences(text)
    if not notes:
        raise InvalidNotes("no notes")
    return notes


def _two_sentences(text: str) -> str:
    parts, out = text.replace("? ", "?|").replace("! ", "!|").replace(". ", ".|").split("|"), []
    for p in parts:
        out.append(p)
        if len(out) == 2:
            break
    text = " ".join(out).strip()
    return text if len(text) <= NOTE_LIMIT else text[: NOTE_LIMIT - 1].rstrip() + "…"


__all__ = ["CHARTS", "CHART_TITLES", "NOT_SET", "STATUS_WORDS", "chart_data", "fingerprint", "has_data",
           "parse_output", "quarter_of", "user_prompt", "SYSTEM_PROMPT", "InvalidNotes", "PROMPT_VERSION"]
