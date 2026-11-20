"""
core/classify.py - T035 (FR-5.2, M5): classification help on Add Regulation.
The SME pastes the regulation's text; the AI suggests the form's Domain,
Country, Region and Business Units. Pure functions: no Firestore or Gemini,
so tests need nothing.

Approved design (Nov 17, OPEN-9):
  - suggestions only: nothing is filled in until the SME clicks Use;
  - Agency is not a field on the form (September form correction), so
    Country and Region are suggested instead;
  - a field the AI is not confident about shows "Not sure" (low confidence
    is never offered as a suggestion);
  - the pasted text is used only for the suggestion and never stored;
  - Domain follows the product or material regulated, not the kind of
    obligation (Srinivas, Nov 19: California SB 343, recyclability labels
    on packaging, is "Packaging", not "Sustainability Transparency").

Kept by code, not left to the AI:
  - Domain must be one of the 17 domains (same list as the form,
    frontend constants/domains.js) or "Other" with a short name;
  - Region must be one of the form's regions or a US state (constants/
    regions.js), or "Other" with a short name;
  - Business Units must be this company's own codes; each needs a reason;
  - every reason is cut short and scrubbed (email addresses, unit codes).
"""
from __future__ import annotations

import json
import re

PROMPT_VERSION = "t035-2"

MIN_CHARS = 200        # roughly a paragraph
MAX_CHARS = 20_000     # longer text is cut (and the answer says so)
REASON_CHARS = 160
NAME_CHARS = 60

# Same lists as the form (frontend constants/domains.js, constants/regions.js).
DOMAINS = [
    "Accessibility", "Basel & Waste Classification", "Batteries", "Chemicals", "Circular Economy",
    "Climate", "Eco Design", "Ecolabels", "Energy", "Extended Producer Responsibility", "Materials",
    "Packaging", "Product Longevity / Repair", "Product Takeback", "Recycling",
    "Sustainability Transparency", "Supply Chain Responsibility",
]
REGIONS = ["APJ", "EU", "EMEA", "Americas", "North America", "Latin America", "Asia", "Europe", "UK"]
US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
    "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas",
    "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island",
    "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
]
CONFIDENCE = ("high", "medium", "low")


class TextTooShort(ValueError):
    message = "Paste more of the regulation (at least a paragraph) for useful suggestions."


class InvalidAnswer(ValueError):
    pass


def prepare_text(text: str) -> tuple[str, bool]:
    """(text to send, was it cut). Raises TextTooShort."""
    text = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) < MIN_CHARS or len(text.split()) < 30:
        raise TextTooShort(TextTooShort.message)
    return (text[:MAX_CHARS], True) if len(text) > MAX_CHARS else (text, False)


SYSTEM_PROMPT = """You help a company sustainability team fill in a regulation form in Regulator.
From the regulation text they pasted, suggest four fields. Use ONLY the pasted text; never use outside knowledge to add facts, and never guess.

Fields:
1. "domain": exactly one of the DOMAINS given, or "Other" with a short "otherName" (2-4 words) when none fits.
   Choose the domain of the products or materials the regulation covers (for example packaging, batteries, chemicals), not the kind of obligation it imposes (labelling, claims, reporting). Example: a law on recyclability labels for packaging is "Packaging". Use "Sustainability Transparency" only when the regulation is mainly about company-level disclosure or reporting rather than a product or material.
2. "country": the country or countries the regulation applies to, as written in plain English (e.g. "United States", "France", "European Union member states"). Empty if the text doesn't say.
3. "region": exactly one of the REGIONS given (US states are allowed when the country is the United States), or "Other" with a short "otherName". Empty if the text doesn't say.
4. "businessUnits": which of the company's BUSINESS UNITS the regulation most likely affects, judged from what each unit's name says it makes or does. Use the unit codes given. Leave it empty if the text gives no basis.

For each field give "confidence": "high" (the text says it plainly), "medium" (strongly implied) or "low" (a guess), and a "reason" of at most 20 words pointing to what in the text supports it. Never suggest a compliance status.

Return JSON only:
{"domain": {"value": "", "otherName": "", "confidence": "", "reason": ""},
 "country": {"value": "", "confidence": "", "reason": ""},
 "region": {"value": "", "otherName": "", "confidence": "", "reason": ""},
 "businessUnits": [{"code": "", "confidence": "", "reason": ""}]}"""


def user_prompt(text: str, units: dict) -> str:
    """units: {code: name} for this company."""
    ctx = {
        "DOMAINS": DOMAINS,
        "REGIONS": REGIONS + ["(US states when the country is the United States)"],
        "BUSINESS_UNITS": [{"code": c, "name": n} for c, n in units.items()],
    }
    return json.dumps(ctx, indent=1) + "\n\nPASTED REGULATION TEXT:\n<<<\n" + text + "\n>>>"


def _s(v) -> str:
    return " ".join(str(v if v is not None else "").split())


def _pick(value: str, options: list[str]) -> str | None:
    key = re.sub(r"[^a-z0-9]+", "", value.lower())
    for opt in options:
        if re.sub(r"[^a-z0-9]+", "", opt.lower()) == key:
            return opt
    return None


def _confidence(item: dict) -> str:
    c = _s(item.get("confidence")).lower()
    return c if c in CONFIDENCE else "low"


def _reason(item: dict, units: dict) -> str:
    from core.summaries import scrub

    reason = scrub(_s(item.get("reason"))[:REASON_CHARS * 2], units)  # scrub first: a name can be longer than its code
    return reason if len(reason) <= REASON_CHARS else reason[: REASON_CHARS - 1].rstrip() + "…"


def _listed_field(item, options: list[str], units: dict) -> dict:
    """Domain / Region: one of the options, or Other with a name; low confidence -> not sure."""
    if not isinstance(item, dict) or not _s(item.get("value")):
        return {"value": None, "notSure": False}
    if _confidence(item) == "low":
        return {"value": None, "notSure": True}
    raw = _s(item.get("value"))
    name = _s(item.get("otherName")) if raw.lower() == "other" else raw  # off-list answer -> Other
    picked = _pick(name, options) if name else None
    other = None if picked else name[:NAME_CHARS]
    if not picked and not other:
        return {"value": None, "notSure": True}
    return {"value": picked or "Other", "otherName": other or "", "confidence": _confidence(item),
            "reason": _reason(item, units), "notSure": False}


def parse_output(answer, units: dict) -> dict:
    if not isinstance(answer, dict):
        raise InvalidAnswer("answer is not an object")
    out = {
        "domain": _listed_field(answer.get("domain"), DOMAINS, units),
        "region": _listed_field(answer.get("region"), REGIONS + US_STATES, units),
    }
    country = answer.get("country") if isinstance(answer.get("country"), dict) else {}
    if not _s(country.get("value")):
        out["country"] = {"value": None, "notSure": False}
    elif _confidence(country) == "low":
        out["country"] = {"value": None, "notSure": True}
    else:
        out["country"] = {"value": _s(country.get("value"))[:NAME_CHARS], "confidence": _confidence(country),
                          "reason": _reason(country, units), "notSure": False}

    bus, seen, unsure = [], set(), False
    for item in answer.get("businessUnits") or []:
        if not isinstance(item, dict):
            continue
        code = _s(item.get("code")).upper()
        if code not in units or code in seen:
            continue
        if _confidence(item) == "low":
            unsure = True
            continue
        seen.add(code)
        bus.append({"code": code, "name": units[code], "confidence": _confidence(item), "reason": _reason(item, units)})
    out["businessUnits"] = {"value": bus, "notSure": unsure and not bus}

    if not any(out[k].get("value") for k in ("domain", "country", "region", "businessUnits")) and not any(
            out[k].get("notSure") for k in out):
        raise InvalidAnswer("nothing usable")
    return out
