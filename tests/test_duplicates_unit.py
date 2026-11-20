"""
T034 (FR-5.1): duplicate check on Add Regulation - text check, AI answer
handling, what the box shows (OPEN-9), POST /regulations/check-duplicates and
the save gate. No server, Firestore or Gemini: fakes throughout.
"""
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core import ai_client, duplicate_routes, duplicates
from core.ai_client import AIResult
from core.deps import get_current_context

REGS = [
    {"id": "eu-bat", "title": "Regulation (EU) 2023/1542 concerning batteries and waste batteries",
     "abbreviatedTitle": "EU Battery Regulation", "dueDate": "2027-02-18", "businessUnitIds": ["BU1"]},
    {"id": "li-ship", "title": "Hazardous materials: lithium battery shipping", "abbreviatedTitle": "49 CFR 173.185",
     "dueDate": "2026-10-21", "businessUnitIds": ["BU2"]},
    {"id": "fr-oil", "title": "French Mineral Oil Ban", "abbreviatedTitle": "AGEC Art. 112", "dueDate": "2026-01-01",
     "businessUnitIds": ["BU1", "BU2"]},
    {"id": "gone", "title": "EU Battery Regulation", "hidden": True},
]


# --- text check ------------------------------------------------------------------

def test_citations_cfr_eu_and_dates():
    assert duplicates.citations("49 CFR 173.185") == {"cfr:49:173.185"}
    assert duplicates.citations("40 C.F.R. Part 60") == {"cfr:40:60"}
    assert duplicates.citations("Regulation (EU) 2023/1542") == {"eu:2023/1542"}
    assert duplicates.citations("Directive 2012/19/EU on WEEE") == {"eu:2012/19"}
    assert duplicates.citations("Due 2026/11/17 in the EU") == set()
    assert duplicates.citations("CA SB 1383") == set()  # bill numbers repeat across states


def test_candidates_by_citation_title_and_similarity_skip_removed():
    def found(title, abbr=""):
        return [(c["reg"]["id"], c["why"]) for c in duplicates.candidates({"title": title, "abbreviated_title": abbr}, REGS)]

    assert found("Batteries regulation 2023/1542 EU") == [("eu-bat", "citation")]
    assert found("EU Battery Passport requirements", "EU Battery Regulation") == [("eu-bat", "title")]
    assert found("Lithium battery transport", "49 CFR 173.185") == [("li-ship", "citation")]
    assert found("Mineral oil ban in France") == [("fr-oil", "similar")]
    assert found("California SB 1383 short-lived climate pollutants") == []


def test_exact_matches_are_only_sure_ones():
    entry = {"title": "Mineral oil ban in France"}
    assert duplicates.exact_matches(entry, REGS) == []
    assert [m["reg"]["id"] for m in duplicates.exact_matches({"title": "x", "abbreviated_title": "eu battery regulation"}, REGS)] == ["eu-bat"]


# --- AI answer ------------------------------------------------------------------

def test_parse_output_keeps_only_asked_ids_and_known_verdicts():
    cands = duplicates.candidates({"title": "Batteries regulation 2023/1542 EU"}, REGS)
    out = duplicates.parse_output({"results": [
        {"id": "eu-bat", "verdict": "SAME", "reason": "Same EU act and due date."},
        {"id": "other-company", "verdict": "same", "reason": "x"},
        {"id": "eu-bat-2", "verdict": "maybe"},
    ]}, cands)
    assert out == {"eu-bat": ("same", "Same EU act and due date.")}
    with pytest.raises(duplicates.InvalidAnswer):
        duplicates.parse_output({"results": []}, cands)
    with pytest.raises(duplicates.InvalidAnswer):
        duplicates.parse_output(["no"], cands)


def test_prompt_has_entry_and_candidates_only():
    cands = duplicates.candidates({"title": "Mineral oil ban in France"}, REGS)
    text = duplicates.user_prompt({"title": "Mineral oil ban in France", "notes": "n" * 2000}, cands)
    assert "fr-oil" in text and "eu-bat" not in text and "n" * 601 not in text


# --- what the box shows (OPEN-9) -----------------------------------------------

def test_decide_with_ai():
    entry = {"title": "Mineral oil ban in France", "business_unit_ids": ["BU3"]}
    cands = duplicates.candidates(entry, REGS)
    same = duplicates.decide(entry, cands, {"fr-oil": ("same", "Same French ban.")})
    assert same["needsConfirmation"] and same["likelyDuplicates"][0]["reason"] == "Same French ban."
    assert same["otherBusinessUnit"] is True  # add BU3 to the existing one instead
    related = duplicates.decide(entry, cands, {"fr-oil": ("related", "An amendment.")})
    assert not related["needsConfirmation"] and related["related"][0]["id"] == "fr-oil"
    different = duplicates.decide(entry, cands, {"fr-oil": ("different", "Different law.")})
    assert different == {"likelyDuplicates": [], "related": [], "needsConfirmation": False, "otherBusinessUnit": False}


def test_decide_without_ai_text_check_alone():
    sure = {"title": "Lithium battery transport", "abbreviated_title": "49 CFR 173.185", "business_unit_ids": ["BU2"]}
    out = duplicates.decide(sure, duplicates.candidates(sure, REGS), None)
    assert out["needsConfirmation"] and out["likelyDuplicates"][0]["matchedBy"] == "citation"
    assert out["otherBusinessUnit"] is False
    weak = {"title": "Mineral oil ban in France"}
    out = duplicates.decide(weak, duplicates.candidates(weak, REGS), None)
    assert not out["needsConfirmation"] and out["related"][0]["id"] == "fr-oil"  # weak is never "same"


def test_ai_saying_different_on_a_sure_match_still_shows_it_as_related():
    entry = {"title": "x", "abbreviated_title": "EU Battery Regulation"}
    out = duplicates.decide(entry, duplicates.candidates(entry, REGS), {"eu-bat": ("different", "Other scope.")})
    assert out["related"][0]["id"] == "eu-bat" and not out["needsConfirmation"]


# --- POST /regulations/check-duplicates and the save gate ------------------------

class Ctx:
    def __init__(self, role="sme"):
        self.uid, self.role, self.tenant_id, self.business_unit_ids = "u1", role, "t1", []
        self.tenant_ref = None


@pytest.fixture
def api(monkeypatch):
    calls = []
    monkeypatch.setattr(duplicate_routes, "_existing", lambda tenant: [r for r in REGS if not r.get("hidden")])

    def fake_generate_json(tenant_id, feature, contents, **kw):
        calls.append(feature)
        if api.error:
            raise api.error
        return {"results": [{"id": "fr-oil", "verdict": "same", "reason": "Same French ban."}]}, AIResult(
            text="{}", model="m", input_tokens=10, output_tokens=10, cost_usd=0.0, finish_reason="STOP")

    monkeypatch.setattr(ai_client, "generate_json", fake_generate_json)
    app = FastAPI()
    app.include_router(duplicate_routes.router)
    api.role = "sme"
    app.dependency_overrides[get_current_context] = lambda: Ctx(api.role)
    api.client, api.calls, api.error = TestClient(app), calls, None
    return api


def check(api, **body):
    return api.client.post("/regulations/check-duplicates", json=body)


def test_nothing_similar_costs_no_ai(api):
    r = check(api, title="California SB 1383 short-lived climate pollutants")
    assert r.status_code == 200 and r.json()["needsConfirmation"] is False and api.calls == []


def test_similar_asks_the_ai_once(api):
    body = check(api, title="Mineral oil ban in France", business_unit_ids=["BU1"]).json()
    assert api.calls == ["duplicate_check"] and body["aiChecked"] and body["needsConfirmation"]
    assert body["likelyDuplicates"][0]["id"] == "fr-oil" and body["otherBusinessUnit"] is False


def test_ai_unavailable_falls_back_to_text_check(api):
    api.error = ai_client.AILimitReached()
    body = check(api, title="Lithium battery transport", abbreviated_title="49 CFR 173.185").json()
    assert body["aiUnavailable"] and body["needsConfirmation"] and body["note"] == duplicate_routes.UNAVAILABLE_NOTE
    weak = check(api, title="Mineral oil ban in France").json()
    assert weak["aiUnavailable"] and not weak["needsConfirmation"] and weak["related"]


def test_users_cannot_check_and_a_title_is_needed(api):
    api.role = "user"
    assert check(api, title="x").status_code == 403
    api.role = "admin"
    assert check(api, title=" ").status_code == 400


def test_save_gate_refuses_unconfirmed_sure_match(monkeypatch):
    monkeypatch.setattr(duplicate_routes, "_existing", lambda tenant: REGS[:3])
    payload = {"title": "Lithium battery transport", "abbreviated_title": "49 CFR 173.185"}
    with pytest.raises(HTTPException) as e:
        duplicate_routes.require_duplicate_confirmation(Ctx(), dict(payload))
    assert e.value.status_code == 409 and e.value.detail["likelyDuplicates"][0]["id"] == "li-ship"

    ok = dict(payload, confirmed_not_duplicate_of=["li-ship", "li-ship", "unknown"])
    assert duplicate_routes.require_duplicate_confirmation(Ctx(), ok) == ["li-ship"]
    assert "confirmed_not_duplicate_of" not in ok  # never saved as a regulation field
    plain = {"title": "Something new entirely"}
    assert duplicate_routes.require_duplicate_confirmation(Ctx(), plain) == []
