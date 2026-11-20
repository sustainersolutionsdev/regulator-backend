"""
core/duplicates.py - T034 (FR-5.1, M5): duplicate / near-duplicate check for
Add Regulation. Pure functions: no Firestore or Gemini, so tests need nothing.

Two stages, so most saves cost no AI at all:

 1. Text check, in code (`candidates`). The new entry is compared with every
    regulation the company already has (all Business Units): shared
    citations ("49 CFR 173.185", "Regulation (EU) 2023/1542", "SB 1383"...),
    the same normalised title or abbreviated name, and title similarity
    (character trigrams). Nothing similar -> nothing to ask the AI.
 2. AI check (`user_prompt` / `parse_output`), only on those few candidates:
    for each, "same" regulation, "related" (amendment, later version, same
    family) or "different", with one short reason.

`decide` turns that into what the screen shows (approved design, OPEN-9):
  likely duplicates -> saving stops until the SME confirms ("Save as a new
                       regulation"), opens the existing one, or edits;
  related           -> a note with links, no extra step;
  AI unavailable    -> the text check alone: a clear title/citation match
                       still asks for confirmation, marked as matched by
                       title; anything weaker is shown as related.
A weak match is never reported as "same". The AI never saves, merges or
removes anything.

`exact_matches` is also used by POST /regulations itself, so the
confirmation cannot be skipped by calling the API directly (FR-5.1:
"requires explicit SME confirmation before the record is saved").
"""
from __future__ import annotations

import re

PROMPT_VERSION = "t034-1"

MAX_CANDIDATES = 5
MIN_SIMILARITY = 0.45     # title similarity worth showing to the AI
STRONG_SIMILARITY = 0.85  # without AI: this close counts as a clear match
NOTES_CHARS = 600

_CFR = re.compile(r"\b(\d{1,3})\s*c\.?\s*f\.?\s*r\.?\s*(?:part\s*|§\s*|sec(?:tion)?\.?\s*)?(\d+(?:\.\d+)?)", re.I)
_USC = re.compile(r"\b(\d{1,3})\s*u\.?\s*s\.?\s*c\.?\s*(?:§\s*)?(\d+[a-z]?)", re.I)
# EU acts: "Regulation (EU) 2023/1542", "Directive 2012/19/EU", "(EC) No 1907/2006",
# or just "2023/1542". A date such as 2026/11/17 is not a citation.
_EU = re.compile(r"(?<![\d/])((?:19|20)\d{2}|\d{1,4})\s*/\s*(\d{1,5})(?!\d)(?!\s*/\s*\d)", re.I)
_EU_CONTEXT = re.compile(r"\b(eu|ec|eec|regulation|directive|decision|no\.?)\b", re.I)
# State bill numbers (SB 1383) repeat across states, so they are left to the
# title comparison and the AI, never treated as a sure match.


def _norm(v) -> str:
    return str(v if v is not None else "").strip()


def normalise(text: str) -> str:
    text = _norm(text).lower()
    text = re.sub(r"[^a-z0-9§/.]+", " ", text)
    return " ".join(text.split())


def citations(*texts: str) -> set[str]:
    """Citation keys found in any of the texts, e.g. {"cfr:49:173.185", "eu:2023/1542"}."""
    found = set()
    for text in texts:
        t = _norm(text)
        if not t:
            continue
        for m in _CFR.finditer(t):
            found.add(f"cfr:{m.group(1)}:{m.group(2)}")
        for m in _USC.finditer(t):
            found.add(f"usc:{m.group(1)}:{m.group(2).lower()}")
        if _EU_CONTEXT.search(t):
            for m in _EU.finditer(t):
                a, b = m.group(1), m.group(2)
                # "(EC) No 1907/2006" puts the number first: order by which part is the year
                if len(b) == 4 and b.startswith(("19", "20")) and not (len(a) == 4 and a.startswith(("19", "20"))):
                    a, b = b, a
                found.add(f"eu:{a}/{int(b)}")
    return found


def _trigrams(text: str) -> set[str]:
    t = f"  {normalise(text)} "
    return {t[i:i + 3] for i in range(len(t) - 2)} if len(t) > 3 else set()


def similarity(a: str, b: str) -> float:
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _names(entry: dict) -> list[str]:
    return [n for n in (_norm(entry.get("title")), _norm(entry.get("abbreviatedTitle") or entry.get("abbreviated_title"))) if n]


def _match(entry: dict, reg: dict) -> tuple[str, float]:
    """(why, score) for one existing regulation; ("", 0) when unrelated."""
    new_names, old_names = _names(entry), _names(reg)
    if not new_names or not old_names:
        return "", 0.0
    shared = citations(*new_names, _norm(entry.get("notes"))) & citations(*old_names, _norm(reg.get("notes")))
    if citations(*new_names) & citations(*old_names):
        return "citation", 1.0
    if {normalise(n) for n in new_names} & {normalise(n) for n in old_names}:
        return "title", 1.0
    best = max(similarity(a, b) for a in new_names for b in old_names)
    if shared and best >= 0.25:
        return "citation", max(best, 0.9)
    return ("similar", best) if best >= MIN_SIMILARITY else ("", 0.0)


def candidates(entry: dict, regs: list[dict], limit: int = MAX_CANDIDATES) -> list[dict]:
    """The most similar existing regulations, best first: [{reg, why, score}]."""
    found = []
    for reg in regs:
        if reg.get("hidden"):
            continue
        why, score = _match(entry, reg)
        if why:
            found.append({"reg": reg, "why": why, "score": round(score, 3)})
    found.sort(key=lambda c: (-c["score"], _norm(c["reg"].get("dueDate")), _norm(c["reg"].get("id"))))
    return found[:limit]


def exact_matches(entry: dict, regs: list[dict]) -> list[dict]:
    """Clear matches the text check alone is sure of: same citation or same title/abbreviated name."""
    return [c for c in candidates(entry, regs, limit=50) if c["why"] in ("citation", "title")]


# --- AI stage ------------------------------------------------------------------

SYSTEM_PROMPT = """You check whether a regulation someone is about to add to Regulator is already there.
Regulator is used by company sustainability teams to track regulations. You get the NEW entry and a few EXISTING regulations from the same company that look similar.

For each existing regulation decide:
- "same": it is the same regulation as the new entry (same law or rule, even if named differently or abbreviated), so adding the new entry would create a duplicate.
- "related": a different entry for a closely connected regulation: an amendment, a later version or phase, an implementing act, or the same rule in another jurisdiction.
- "different": not the same regulation.

Rules:
1. Use only what is given. Do not use outside knowledge to invent citations, dates or facts.
2. Different due dates alone do not make two entries different, but different jurisdictions or different citations usually do.
3. If you are not sure it is the same regulation, answer "related", never "same".
4. Give one short plain-English reason (at most 25 words) for each.

Return JSON only: {"results": [{"id": "<existing id>", "verdict": "same" | "related" | "different", "reason": "..."}]}"""


def _brief(reg: dict) -> dict:
    return {
        "title": _norm(reg.get("title")),
        "abbreviatedName": _norm(reg.get("abbreviatedTitle") or reg.get("abbreviated_title")),
        "dueDate": _norm(reg.get("dueDate") or reg.get("due_date")),
        "domain": _norm(reg.get("domain")),
        "country": _norm(reg.get("country")),
        "region": _norm(reg.get("region")),
        "notes": _norm(reg.get("notes"))[:NOTES_CHARS],
    }


def user_prompt(entry: dict, cands: list[dict]) -> str:
    import json

    existing = [{"id": c["reg"]["id"], **_brief(c["reg"])} for c in cands]
    return "NEW entry:\n" + json.dumps(_brief(entry), indent=1) + "\n\nEXISTING regulations:\n" + json.dumps(existing, indent=1)


class InvalidAnswer(ValueError):
    pass


def parse_output(answer, cands: list[dict]) -> dict:
    """{existing id: (verdict, reason)} for the ids that were asked about."""
    if not isinstance(answer, dict) or not isinstance(answer.get("results"), list):
        raise InvalidAnswer("answer has no results list")
    asked = {c["reg"]["id"] for c in cands}
    out = {}
    for item in answer["results"]:
        if not isinstance(item, dict):
            continue
        rid, verdict = _norm(item.get("id")), _norm(item.get("verdict")).lower()
        if rid in asked and verdict in ("same", "related", "different"):
            reason = " ".join(_norm(item.get("reason")).split())[:200]
            out[rid] = (verdict, reason)
    if not out:
        raise InvalidAnswer("no usable verdicts")
    return out


# --- What the screen shows -----------------------------------------------------

WHY_TEXT = {
    "citation": "Same citation as what you entered.",
    "title": "Same title or abbreviated name as what you entered.",
    "similar": "Similar title.",
}


def _view(c: dict, reason: str) -> dict:
    reg = c["reg"]
    return {
        "id": reg["id"],
        "title": _norm(reg.get("title")),
        "abbreviatedTitle": _norm(reg.get("abbreviatedTitle")),
        "dueDate": _norm(reg.get("dueDate")),
        "businessUnitIds": list(reg.get("businessUnitIds") or []),
        "reason": reason,
        "matchedBy": c["why"],
    }


def decide(entry: dict, cands: list[dict], verdicts: dict | None) -> dict:
    """
    verdicts: the AI's {id: (verdict, reason)}, or None when the AI was not
    used or not available (OPEN-9: the text check alone decides).
    """
    duplicates, related = [], []
    for c in cands:
        rid = c["reg"]["id"]
        if verdicts is not None and rid in verdicts:
            verdict, reason = verdicts[rid]
            if verdict == "same":
                duplicates.append(_view(c, reason or WHY_TEXT[c["why"]]))
            elif verdict == "related" or c["why"] in ("citation", "title"):
                related.append(_view(c, reason or WHY_TEXT[c["why"]]))
        elif c["why"] in ("citation", "title") or c["score"] >= STRONG_SIMILARITY:
            duplicates.append(_view(c, WHY_TEXT[c["why"]] if c["why"] != "similar" else "Very similar title."))
        else:
            related.append(_view(c, WHY_TEXT[c["why"]]))

    new_units = set(entry.get("businessUnitIds") or entry.get("business_unit_ids") or [])
    other_unit = any(d["businessUnitIds"] and not (new_units & set(d["businessUnitIds"])) for d in duplicates)
    return {
        "likelyDuplicates": duplicates,
        "related": related,
        "needsConfirmation": bool(duplicates),
        "otherBusinessUnit": other_unit,
    }
