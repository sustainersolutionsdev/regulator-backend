"""
T035 (FR-5.2): classification help on Add Regulation - text checks, what the
AI's answer may contain, and POST /regulations/suggest-fields (OPEN-9). No
server, Firestore or Gemini: fakes throughout.
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import ai_client, classify, classify_routes
from core.ai_client import AIResult
from core.deps import get_current_context

UNITS = {"BU1": "Printers", "BU2": "Computers", "BU3": "Data Centers"}
TEXT = ("Regulation (EU) 2023/1542 of the European Parliament and of the Council concerning batteries and waste "
        "batteries. From 18 February 2027, LMT batteries, industrial batteries with a capacity greater than 2 kWh "
        "and electric vehicle batteries placed on the market shall have an electronic record (battery passport). "
        "Economic operators shall make the information accessible through a QR code printed on the battery.")


def answer(**over):
    base = {
        "domain": {"value": "Batteries", "confidence": "high", "reason": "The text is about batteries and waste batteries."},
        "country": {"value": "European Union member states", "confidence": "high", "reason": "An EU regulation."},
        "region": {"value": "EU", "confidence": "high", "reason": "European Parliament and Council."},
        "businessUnits": [{"code": "BU3", "confidence": "medium", "reason": "Industrial batteries above 2 kWh, as in data centers."}],
    }
    base.update(over)
    return base


# --- text ------------------------------------------------------------------------

def test_too_little_text_is_refused_and_long_text_is_cut():
    with pytest.raises(classify.TextTooShort):
        classify.prepare_text("Batteries regulation.")
    text, cut = classify.prepare_text(TEXT)
    assert not cut and text.startswith("Regulation (EU)")
    text, cut = classify.prepare_text(TEXT * 100)
    assert cut and len(text) == classify.MAX_CHARS


def test_prompt_has_lists_units_and_text():
    prompt = classify.user_prompt(TEXT, UNITS)
    data = json.loads(prompt.split("\n\nPASTED")[0])
    assert len(data["DOMAINS"]) == 17 and "Product Longevity / Repair" in data["DOMAINS"]
    assert {"code": "BU3", "name": "Data Centers"} in data["BUSINESS_UNITS"]
    assert TEXT in prompt


# --- what the answer may contain ---------------------------------------------------

def test_good_answer():
    out = classify.parse_output(answer(), UNITS)
    assert out["domain"] == {"value": "Batteries", "otherName": "", "confidence": "high",
                             "reason": "The text is about batteries and waste batteries.", "notSure": False}
    assert out["region"]["value"] == "EU" and out["country"]["value"] == "European Union member states"
    assert out["businessUnits"]["value"] == [{"code": "BU3", "name": "Data Centers", "confidence": "medium",
                                              "reason": "Industrial batteries above 2 kWh, as in data centers."}]


def test_lists_are_matched_loosely_and_off_list_becomes_other():
    out = classify.parse_output(answer(domain={"value": "product longevity/repair", "confidence": "high"},
                                       region={"value": "california", "confidence": "medium"}), UNITS)
    assert out["domain"]["value"] == "Product Longevity / Repair" and out["region"]["value"] == "California"
    out = classify.parse_output(answer(domain={"value": "Other", "otherName": "Water", "confidence": "medium"},
                                       region={"value": "Middle East", "confidence": "high"}), UNITS)
    assert (out["domain"]["value"], out["domain"]["otherName"]) == ("Other", "Water")
    assert (out["region"]["value"], out["region"]["otherName"]) == ("Other", "Middle East")
    out = classify.parse_output(answer(domain={"value": "Other", "otherName": "batteries", "confidence": "high"}), UNITS)
    assert out["domain"]["value"] == "Batteries" and out["domain"]["otherName"] == ""


def test_low_confidence_is_not_sure_and_empty_is_no_suggestion():
    out = classify.parse_output(answer(country={"value": "France", "confidence": "low"},
                                       region={"value": "", "confidence": "high"}), UNITS)
    assert out["country"] == {"value": None, "notSure": True}
    assert out["region"] == {"value": None, "notSure": False}


def test_business_units_only_own_codes_no_repeats_low_is_not_sure():
    out = classify.parse_output(answer(businessUnits=[
        {"code": "bu1", "confidence": "high", "reason": "Printer batteries."},
        {"code": "BU1", "confidence": "high", "reason": "again"},
        {"code": "BU9", "confidence": "high", "reason": "another company"},
        {"code": "BU2", "confidence": "low", "reason": "maybe"},
    ]), UNITS)
    assert [b["code"] for b in out["businessUnits"]["value"]] == ["BU1"] and out["businessUnits"]["notSure"] is False
    out = classify.parse_output(answer(businessUnits=[{"code": "BU2", "confidence": "low"}]), UNITS)
    assert out["businessUnits"] == {"value": [], "notSure": True}


def test_reasons_are_short_and_scrubbed():
    out = classify.parse_output(answer(domain={"value": "Batteries", "confidence": "high",
                                               "reason": "BU3 contact a@b.com " + "x" * 300}), UNITS)
    reason = out["domain"]["reason"]
    assert "@" not in reason and "BU3" not in reason and reason.startswith("Data Centers contact") and len(reason) <= 160


def test_unusable_answers_are_refused():
    with pytest.raises(classify.InvalidAnswer):
        classify.parse_output(["no"], UNITS)
    with pytest.raises(classify.InvalidAnswer):
        classify.parse_output({"domain": {}, "country": {}, "region": {}, "businessUnits": []}, UNITS)


# --- POST /regulations/suggest-fields ---------------------------------------------

class Snap:
    def __init__(self, doc_id, data):
        self.id, self._data = doc_id, data

    def to_dict(self):
        return dict(self._data)


class Coll:
    def stream(self):
        return [Snap(c, {"label": n}) for c, n in UNITS.items()]


class TenantRef:
    def collection(self, name):
        assert name == "businessUnits"
        return Coll()


class Ctx:
    def __init__(self, role):
        self.uid, self.role, self.tenant_id, self.business_unit_ids = "u1", role, "t1", []
        self.tenant_ref = TenantRef()


@pytest.fixture
def api(monkeypatch):
    calls = []

    def fake_generate_json(tenant_id, feature, contents, **kw):
        calls.append((tenant_id, feature))
        if api.error:
            raise api.error
        return api.answer, AIResult(text="{}", model="m", input_tokens=10, output_tokens=10, cost_usd=0.0, finish_reason="STOP")

    monkeypatch.setattr(ai_client, "generate_json", fake_generate_json)
    app = FastAPI()
    app.include_router(classify_routes.router)
    api.role, api.error, api.answer = "sme", None, answer()
    app.dependency_overrides[get_current_context] = lambda: Ctx(api.role)
    api.client, api.calls = TestClient(app), calls
    return api


def suggest(api, text=TEXT):
    return api.client.post("/regulations/suggest-fields", json={"text": text})


def test_suggestions(api):
    body = suggest(api).json()
    assert body["aiAvailable"] and body["suggestions"]["domain"]["value"] == "Batteries"
    assert api.calls == [("t1", "classify")]


def test_short_text_costs_no_ai(api):
    r = suggest(api, "Batteries.")
    assert r.status_code == 400 and "at least a paragraph" in r.json()["detail"] and api.calls == []


def test_ai_unavailable_or_limit_is_a_plain_message(api):
    api.error = ai_client.AINoAnswer()
    body = suggest(api).json()
    assert body == {"aiAvailable": False, "message": classify_routes.UNAVAILABLE, "suggestions": None, "textCut": False}
    api.error = ai_client.AILimitReached()
    assert "today's AI limit" in suggest(api).json()["message"]
    api.error, api.answer = None, {"domain": {}, "businessUnits": []}
    assert suggest(api).json()["aiAvailable"] is False


def test_users_cannot_ask(api):
    api.role = "user"
    assert suggest(api).status_code == 403
