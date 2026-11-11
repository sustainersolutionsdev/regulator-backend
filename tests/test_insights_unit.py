"""
T031: AI summaries on the Insights page - filters, entries, length budget,
answer checks and POST /insights/summary. No server, Firestore or Gemini
needed: fakes throughout. Never calls Gemini.
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import ai_client, insights, insight_routes
from core.ai_client import AIResult
from core.deps import get_current_context

F0 = insights.clean_filters({})
NAMES = {"BU1": "Printers", "BU2": "Computers", "BU3": "Data Centers"}


def reg(rid, due, units, statuses=None, **kw):
    return {"id": rid, "title": f"Regulation {rid}", "abbreviatedTitle": rid.upper(), "dueDate": due,
            "domain": kw.pop("domain", "Batteries"), "country": kw.pop("country", "US"), "region": kw.pop("region", ""),
            "businessUnitIds": units, "buStatuses": statuses or {}, "addToRoadmapDashboard": True,
            "documents": kw.pop("documents", []), "notes": kw.pop("notes", ""), **kw}


REGS = [
    reg("a", "2026-03-01", ["BU1", "BU2", "BU3"],
        {"BU1": {"status": "action_needed", "note": "Supplier data missing; escalated."}, "BU2": {"status": "compliant", "note": "Done."}}),
    reg("b", "2026-07-01", ["BU2"], {"BU2": {"status": "pending", "note": "Survey 60% returned."}}, domain="Chemicals", region="Europe"),
    reg("c", "2027-02-18", ["BU3"], {"BU3": {"status": "action_needed", "note": "No owner."}}, country=""),
    reg("d", "2026-05-01", ["BU1"], {"BU1": {"status": "action_needed"}}, hidden=True),
    {**reg("e", "2026-05-01", ["BU1"]), "addToRoadmapDashboard": False},
]


# --- filters ------------------------------------------------------------------

def test_clean_filters_checks_and_normalises():
    f = insights.clean_filters({"domain": [" Batteries ", "Batteries"], "dueFrom": "2026-01-01", "docs": "maybe"})
    assert f["domain"] == ["Batteries"] and f["dueFrom"] == "2026-01-01" and f["docs"] == ""
    for bad in ({"dueTo": "31/12/2026"}, {"q": "x" * 201}, {"bu": "BU1"}, {"region": [1]}):
        with pytest.raises(insights.BadFilters):
            insights.clean_filters(bad)


def test_filter_regulations_matches_the_page():
    ids = lambda f: [r["id"] for r in insights.filter_regulations(REGS, insights.clean_filters(f))]  # noqa: E731
    assert ids({}) == ["a", "b", "c"]  # hidden and off-Dashboard left out
    assert ids({"dueFrom": "2026-01-01", "dueTo": "2026-12-31"}) == ["a", "b"]
    assert ids({"bu": ["BU3"]}) == ["a", "c"]
    assert ids({"country": [insights.NOT_SET]}) == ["c"]
    assert ids({"region": ["Europe"], "domain": ["Chemicals"]}) == ["b"]
    assert ids({"q": "survey"}) == ["b"]  # search covers unit explanations
    docs_reg = [{**REGS[0], "documents": [{"hidden": True}]}, {**REGS[1], "documents": [{"hidden": False}]}]
    assert [r["id"] for r in insights.filter_regulations(docs_reg, insights.clean_filters({"docs": "with"}))] == ["b"]


# --- entries and budget -------------------------------------------------------

def test_entries_count_once_per_unit_red_first():
    regs = insights.filter_regulations(REGS, F0)
    all_entries = insights.build_entries(regs, F0, "all", NAMES)
    assert len(all_entries) == 5 and insights.counts(all_entries) == {"statuses": 5, "regulations": 3}
    assert [(e["regulationId"], e["unit"]) for e in all_entries] == [
        ("a", "Printers"), ("c", "Data Centers"), ("b", "Computers"), ("a", "Data Centers"), ("a", "Computers")]
    assert all_entries[3]["statusLabel"] == "Not set" and all_entries[0]["statusLabel"] == "red (Action needed)"
    red = insights.build_entries(regs, F0, "action_needed", NAMES)
    assert [e["regulationId"] for e in red] == ["a", "c"]
    only_bu3 = insights.clean_filters({"bu": ["BU3"]})
    assert [(e["regulationId"], e["unitCode"]) for e in insights.build_entries(insights.filter_regulations(REGS, only_bu3), only_bu3, "all", NAMES)] == [("c", "BU3"), ("a", "BU3")]


def _entry(i, status, due="2026-06-01"):
    return {"regulationId": f"r{i}", "name": f"R{i}", "title": f"R{i}", "dueDate": due, "unit": "U", "unitCode": "U",
            "status": status, "statusLabel": status, "explanation": "", "smeNotes": "", "domain": ""}


def test_budget_keeps_every_red_then_fills_by_order(monkeypatch):
    monkeypatch.setattr(insights, "MAX_WORDS", 200)
    entries = [_entry(i, "action_needed") for i in range(3)] + [_entry(10 + i, "compliant") for i in range(10)]
    kept, more = insights.plan_budget(entries)
    # 3 reds = 240 words (over 200) are all kept; no room for any compliant line.
    assert [e["status"] for e in kept] == ["action_needed"] * 3 and len(more) == 10
    assert [e["id"] for e in kept] == ["e1", "e2", "e3"]
    monkeypatch.setattr(insights, "MAX_WORDS", 300)
    kept, more = insights.plan_budget([_entry(i, "action_needed") for i in range(3)] + [_entry(10 + i, "compliant") for i in range(10)])
    assert len(kept) == 6 and len(more) == 7  # 240 + 3 x 20


def test_red_has_a_hard_stop(monkeypatch):
    monkeypatch.setattr(insights, "MAX_RED", 2)
    kept, more = insights.plan_budget([_entry(i, "action_needed") for i in range(5)])
    assert len(kept) == 2 and len(more) == 3


def test_parse_output_falls_back_and_trims():
    kept = [{**_entry(1, "action_needed"), "id": "e1", "explanation": "Supplier data missing."},
            {**_entry(2, "compliant"), "id": "e2"}]
    out = insights.parse_output({"short": "Two statuses.", "overview": "", "entries": [{"id": "e2", "text": "x " * 200}]}, kept)
    assert out["entries"][0] == {"id": "e1", "text": "Supplier data missing."}
    assert len(out["entries"][1]["text"]) <= 20 * 9 and out["entries"][1]["text"].endswith("…")
    with pytest.raises(insights.InvalidSummary):
        insights.parse_output({"short": ""}, kept)
    with pytest.raises(insights.InvalidSummary):
        insights.parse_output([], kept)


def test_view_label_uses_unit_names_and_cache_keys_differ():
    f = insights.clean_filters({"bu": ["BU1"], "country": [insights.NOT_SET], "dueFrom": "2026-01-01"})
    label = insights.view_label(f, NAMES)
    assert "Business Unit: Printers" in label and "BU1" not in label and "country: Not specified" in label
    assert insights.view_label(F0) == "all regulations"
    assert insights.filters_key(F0, "all") != insights.filters_key(F0, "pending") != insights.filters_key(f, "pending")


def test_prompt_holds_names_not_codes_or_emails():
    regs = insights.filter_regulations(REGS, F0)
    kept, more = insights.plan_budget(insights.build_entries(regs, F0, "all", NAMES))
    text = insights.user_prompt("all", kept, more, {"statuses": 5, "regulations": 3}, "all regulations")
    data = json.loads(text.split("DATA:\n", 1)[1])
    assert {e["businessUnit"] for e in data["entries"]} == {"Printers", "Computers", "Data Centers"}
    assert "BU1" not in text and "@" not in text and data["entries"][0]["words"] == 80


# --- POST /insights/summary ----------------------------------------------------

class FakeSnap:
    def __init__(self, doc_id, data):
        self.id, self._data, self.exists = doc_id, data, data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeDoc:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def collection(self, name):
        return FakeCol(self.db, self.path + (name,))

    def get(self):
        return FakeSnap(self.path[-1], self.db.docs.get(self.path))

    def set(self, data, merge=False):
        self.db.docs[self.path] = dict(data)


class FakeCol:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def document(self, name):
        return FakeDoc(self.db, self.path + (name,))

    def stream(self):
        n = len(self.path)
        return [FakeSnap(p[-1], d) for p, d in sorted(self.db.docs.items()) if p[:n] == self.path and len(p) == n + 1]


class FakeDb:
    def __init__(self):
        self.docs = {}

    def collection(self, name):
        return FakeCol(self, (name,))


class Ctx:
    def __init__(self, db, tenant="t1"):
        self.uid, self.role, self.tenant_id, self.business_unit_ids = "u1", "user", tenant, ["BU1"]
        self.tenant_ref = db.collection("tenants").document(tenant)


@pytest.fixture
def api(monkeypatch):
    db = FakeDb()
    t = ("tenants", "t1")
    for r in REGS:
        db.docs[t + ("regulations", r["id"])] = {k: v for k, v in r.items() if k != "id"}
    for code, label in NAMES.items():
        db.docs[t + ("businessUnits", code)] = {"label": label}
    db.docs[t + ("users", "u-ravi")] = {"displayName": "Ravi Menon", "email": "ravi@acme.example", "role": "user", "businessUnitIds": ["BU1"]}
    db.docs[("tenants", "t2", "regulations", "x")] = {**{k: v for k, v in REGS[0].items() if k != "id"}}  # another company
    calls = []

    def fake_generate_json(tenant_id, feature, contents, **kw):
        calls.append({"tenant": tenant_id, "feature": feature, "contents": contents})
        if api.error:
            raise api.error
        data = json.loads(contents.split("DATA:\n", 1)[1])
        answer = {"short": f"{len(data['entries'])} statuses; Printers is red (Action needed).", "overview": "Overview.",
                  "entries": [{"id": e["id"], "text": f"Line for {e['regulation']}"} for e in data["entries"]]}
        return answer, AIResult(text="{}", model="gemini-3.1-flash-lite", input_tokens=500, output_tokens=200, cost_usd=0.0004, finish_reason="STOP")

    monkeypatch.setattr(ai_client, "generate_json", fake_generate_json)
    app = FastAPI()
    app.include_router(insight_routes.router)
    app.dependency_overrides[get_current_context] = lambda: Ctx(db)
    api.client, api.db, api.calls, api.error = TestClient(app), db, calls, None
    return api


def post(api, group, filters=None):
    return api.client.post("/insights/summary", json={"group": group, "filters": filters or {}})


def test_red_row_generates_then_reuses(api):
    r = post(api, "action_needed")
    assert r.status_code == 200
    body = r.json()
    assert body["aiGenerated"] is True and body["cached"] is False and "legal advice" in body["disclaimer"]
    assert body["counts"] == {"statuses": 2, "regulations": 2}
    assert [e["name"] for e in body["entries"]] == ["A", "C"] and body["entries"][0]["text"] == "Line for A"
    assert body["entries"][0]["contacts"] == [{"name": "Ravi Menon", "email": "ravi@acme.example"}]
    assert api.calls[0]["tenant"] == "t1" and api.calls[0]["feature"] == "insights_action_needed"
    again = post(api, "action_needed").json()
    assert again["cached"] is True and len(api.calls) == 1 and again["short"] == body["short"]


def test_change_regenerates_and_views_are_separate(api):
    post(api, "all")
    api.db.docs[("tenants", "t1", "regulations", "b")]["buStatuses"]["BU2"]["note"] = "Survey 90% returned."
    assert post(api, "all").json()["cached"] is False and len(api.calls) == 2
    post(api, "all", {"dueFrom": "2026-01-01", "dueTo": "2026-12-31"})
    assert len(api.calls) == 3  # another view is its own summary


def test_contacts_only_for_red_and_not_set(api):
    body = post(api, "all").json()
    by = {(e["name"], e["unit"]): e for e in body["entries"]}
    assert by[("A", "Printers")]["contacts"] and by[("A", "Computers")]["contacts"] == []
    assert by[("A", "Data Centers")]["status"] is None and by[("A", "Data Centers")]["statusLabel"] == "Not set"


def test_empty_row_needs_no_ai(api):
    body = post(api, "compliant", {"bu": ["BU3"]}).json()
    assert body["aiGenerated"] is False and body["counts"]["statuses"] == 0
    assert body["short"] == "No Business Unit statuses are Compliant in this view." and api.calls == []


def test_server_applies_filters_and_never_reads_other_companies(api):
    body = post(api, "all", {"domain": ["Chemicals"]}).json()
    assert body["counts"] == {"statuses": 1, "regulations": 1} and body["entries"][0]["name"] == "B"
    assert "Regulation x" not in api.calls[0]["contents"]


def test_bad_requests(api):
    assert post(api, "everything").status_code == 400
    assert post(api, "all", {"dueFrom": "tomorrow"}).status_code == 400
    assert api.calls == []


@pytest.mark.parametrize("error,status", [(ai_client.AILimitReached(), 429), (ai_client.AICreditsExhausted("402"), 503)])
def test_ai_problems_show_their_message(api, error, status):
    api.error = error
    r = post(api, "action_needed")
    assert r.status_code == status and r.json()["detail"] == error.message
    assert not any(p[2] == "aiInsightSummaries" for p in api.db.docs if len(p) > 2)
