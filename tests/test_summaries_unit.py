"""
T029: single-regulation AI summary - document reading, inputs, fingerprint,
answer checks and GET /regulations/{id}/summary. No server, Firestore,
Cloud Storage or Gemini needed: fakes throughout. Never calls Gemini.
"""
import datetime as dt
import io
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import ai_client, doc_reader, summaries, summary_routes
from core.ai_client import AIResult
from core.deps import get_current_context
from core.regulation_routes import document_key

T0 = dt.datetime(2026, 11, 1, 9, 0, tzinfo=dt.timezone.utc)


# --- document reading ---------------------------------------------------------

def _docx(text):
    import docx
    d = docx.Document()
    d.add_paragraph(text)
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text, t.rows[0].cells[1].text = "Model", "UPS-3"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _xlsx():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Suppliers"
    ws.append(["Supplier", "Passport data"])
    ws.append(["Acme Cells", "Ready"])
    ws.append([None, None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _pptx():
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Passport readiness"
    s.notes_slide.notes_text_frame.text = "Two models outstanding"
    s.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1)).text_frame.text = "1 of 3 models ready"
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_reads_docx_xlsx_pptx_txt():
    r = doc_reader.read_document(_docx("Supplier letter: data ready for 1 of 3 models."), "letter.docx")
    assert r.kind == "text" and "1 of 3 models" in r.text and "Model | UPS-3" in r.text
    r = doc_reader.read_document(_xlsx(), "suppliers.xlsx")
    assert r.kind == "text" and "[Sheet: Suppliers]" in r.text and "Acme Cells | Ready" in r.text
    r = doc_reader.read_document(_pptx(), "deck.pptx")
    assert r.kind == "text" and "Passport readiness" in r.text and "Notes: Two models outstanding" in r.text
    r = doc_reader.read_document("Survey 60% returned\n\n".encode(), "notes.TXT")
    assert r.kind == "text" and r.text == "Survey 60% returned"


def test_pdf_is_passed_through_within_limit(monkeypatch):
    assert doc_reader.read_document(b"%PDF-1.7 ...", "a.pdf").kind == "pdf"
    assert doc_reader.read_document(b"<html>", "a.pdf").reason == "not a valid PDF"
    monkeypatch.setattr(doc_reader, "MAX_PDF_BYTES", 10)
    assert "larger than" in doc_reader.read_document(b"%PDF-1.7 lots of bytes", "a.pdf").reason


@pytest.mark.parametrize("name,word", [("a.doc", "Word"), ("a.xls", "Excel"), ("a.ppt", "PowerPoint")])
def test_older_office_formats_are_not_read(name, word):
    r = doc_reader.read_document(b"\xd0\xcf\x11\xe0", name)
    assert r.kind == "unreadable" and word in r.reason


def test_damaged_empty_and_long_documents(monkeypatch):
    assert doc_reader.read_document(b"not a zip", "x.docx").reason == "the file could not be opened"
    assert "no readable text" in doc_reader.read_document(b"   \n ", "x.txt").reason
    monkeypatch.setattr(doc_reader, "MAX_TEXT_CHARS", 20)
    r = doc_reader.read_document(b"0123456789" * 5, "x.txt")
    assert r.partial and len(r.text) == 20


# --- inputs, fingerprint, answer checks ---------------------------------------

def uri(name):
    return f"gs://regulator-dev-documents/tenants/t1/regulation-documents/{name}"


def make_reg(**over):
    reg = {
        "title": "EU Batteries Regulation (EU) 2023/1542 - Battery Passport (Art. 77)",
        "abbreviatedTitle": "EU Battery Passport", "dueDate": "2027-02-18", "domain": "Batteries",
        "country": "European Union", "region": "Europe",
        "notes": "From 18 Feb 2027 industrial batteries above 2 kWh need a digital passport.",
        "businessUnitIds": ["BU2", "BU3"],
        "buStatuses": {
            "BU2": {"status": "compliant", "note": "Laptop batteries are portable; no passport needed."},
            "BU3": {"status": "action_needed", "note": "Supplier cannot give passport data for 2 of 3 models."},
        },
        "documents": [
            {"fileName": "letter.docx", "gcsUri": uri("letter.docx"), "buCode": "BU3"},
            {"fileName": "survey.doc", "gcsUri": uri("survey.doc")},
            {"fileName": "old.pdf", "gcsUri": uri("old.pdf")},
        ],
        "hiddenDocuments": {document_key(uri("old.pdf")): {"hiddenAt": T0}},
    }
    reg.update(over)
    return reg


def flagged(reg):
    from core.regulation_routes import with_document_flags
    return with_document_flags(reg)


LABELS = {"BU2": "Computers", "BU3": "Data Centers"}
PEOPLE = {"u-ravi": {"displayName": "Ravi Menon", "email": "ravi@acme.example"}}
ENTRIES = [
    {"id": "a1", "buCode": "BU3", "actorUid": "u-ravi", "at": T0, "note": "Escalated to procurement.", "removed": False},
    {"id": "a2", "buCode": "BU3", "actorUid": "u-ravi", "at": T0 + dt.timedelta(days=1), "note": "Removed note", "removed": True},
    {"id": "a3", "buCode": "BU3", "actorUid": "gone", "at": T0 + dt.timedelta(days=2), "note": "Decision due 15 Dec.", "removed": False},
]


def test_facts_hold_only_entered_information():
    f = summaries.build_facts(flagged(make_reg()), ENTRIES, LABELS, PEOPLE)
    bu3 = f["units"][1]
    assert bu3["name"] == "Data Centers" and bu3["statusLabel"] == "red (Action needed)"
    assert [n["note"] for n in bu3["recentNotes"]] == ["Decision due 15 Dec.", "Escalated to procurement."]
    assert bu3["recentNotes"][0]["by"] == "a former user" and bu3["recentNotes"][1]["date"] == "2026-11-01"
    assert [d["fileName"] for d in f["documents"]] == ["letter.docx", "survey.doc"]  # hidden PDF left out
    assert f["documents"][0]["scope"] == "Data Centers" and f["documents"][1]["scope"] == "Whole regulation"
    assert "@" not in json.dumps(f)  # no email addresses go to the AI


def test_status_labels_and_not_set():
    assert summaries.status_label("pending") == "amber (Pending)"
    assert summaries.status_label(None) == summaries.status_label("weird") == "Not set"


def test_fingerprint_changes_with_any_input_model_or_prompt(monkeypatch):
    base = summaries.build_facts(flagged(make_reg()), ENTRIES, LABELS, PEOPLE)
    fp = summaries.fingerprint(base, "gemini-3.1-flash-lite")
    assert fp == summaries.fingerprint(summaries.build_facts(flagged(make_reg()), ENTRIES, LABELS, PEOPLE),
                                       "gemini-3.1-flash-lite")
    changed = make_reg(buStatuses={**make_reg()["buStatuses"], "BU2": {"status": "pending", "note": "x"}})
    assert fp != summaries.fingerprint(summaries.build_facts(flagged(changed), ENTRIES, LABELS, PEOPLE), "gemini-3.1-flash-lite")
    assert fp != summaries.fingerprint(base, "gemini-3.8-flash")
    monkeypatch.setattr(summaries, "PROMPT_VERSION", "changed-for-test")
    assert fp != summaries.fingerprint(base, "gemini-3.1-flash-lite")


GOOD = {
    "short": "Battery passport due 18 Feb 2027. Data Centers is red (Action needed); Computers is green (Compliant).",
    "regulation": "From 18 Feb 2027 industrial batteries above 2 kWh need a digital passport.",
    "documentsAndNotes": "The supplier letter says data is ready for 1 of 3 models.",
    "units": [{"code": "BU2", "summary": "No passport needed."},
              {"code": "BU3", "summary": "Data missing for 2 models."},
              {"code": "BU9", "summary": "invented unit"}],
    "actionsNeeded": [{"code": "BU2", "action": "Keep QR labels on track."},
                      {"code": "BU3", "action": "Decide on a supplier by 15 Dec."},
                      {"code": "BU9", "action": "Unknown unit action."}],
}


def test_parse_output_fixes_order_labels_and_unknown_units():
    f = summaries.build_facts(flagged(make_reg()), ENTRIES, LABELS, PEOPLE)
    s = summaries.parse_output(GOOD, f, [{"fileName": "survey.doc", "reason": "older Word format (.doc)"}])
    assert [u["code"] for u in s["units"]] == ["BU3", "BU2"]  # red first
    assert s["units"][0]["statusLabel"] == "red (Action needed)"  # from stored status, not the AI
    assert [a["code"] for a in s["actionsNeeded"]] == ["BU3", "", "BU2"]  # red first; unknown -> whole regulation
    assert s["actionsNeeded"][0]["unit"] == "Data Centers"
    assert s["documentsAndNotes"].endswith("1 document could not be read: survey.doc (older Word format (.doc)).")


@pytest.mark.parametrize("bad", [None, [], "text", {"short": "", "regulation": "x"}, {"short": "x"}])
def test_unusable_answers_are_rejected(bad):
    f = summaries.build_facts(flagged(make_reg()), [], LABELS, PEOPLE)
    with pytest.raises(summaries.InvalidSummary):
        summaries.parse_output(bad, f, [])


def test_long_text_is_trimmed_and_missing_unit_lines_are_explained():
    f = summaries.build_facts(flagged(make_reg(buStatuses={})), [], LABELS, PEOPLE)
    s = summaries.parse_output({"short": "x " * 400, "regulation": "y", "units": []}, f, [])
    assert len(s["short"]) <= summaries.LIMITS["short"] and s["short"].endswith("…")
    assert all(u["summary"] == "No explanation has been entered." and u["statusLabel"] == "Not set" for u in s["units"])
    assert s["documentsAndNotes"] == "No documents or notes have been added."


def test_contacts_are_assigned_users_only():
    users = {
        "u1": {"displayName": "Ravi Menon", "email": "ravi@acme.example", "role": "user", "businessUnitIds": ["BU3"]},
        "u2": {"displayName": "Anita Rao", "email": "anita@acme.example", "role": "user", "businessUnitIds": ["BU2", "BU3"]},
        "u3": {"displayName": "Priya Shah", "email": "priya@acme.example", "role": "sme", "businessUnitIds": ["BU2", "BU3"]},
        "u4": {"displayName": "Gone", "email": "gone@acme.example", "role": "user", "businessUnitIds": ["BU3"], "hidden": True},
    }
    c = summaries.contacts_by_unit(users, ["BU2", "BU3"])
    assert [p["name"] for p in c["BU3"]] == ["Anita Rao", "Ravi Menon"] and [p["name"] for p in c["BU2"]] == ["Anita Rao"]


def test_prompt_parts_carry_documents():
    f = summaries.build_facts(flagged(make_reg()), ENTRIES, LABELS, PEOPLE)
    parts = summaries.user_parts(f, [
        {"fileName": "letter.docx", "scope": "Data Centers", "kind": "text", "text": "ready for 1 of 3", "partial": True},
        {"fileName": "a.pdf", "scope": "Whole regulation", "kind": "pdf", "data_b64": "JVBERg=="},
    ])
    assert "red (Action needed)" in parts[0] and "Data Centers" in parts[0]
    assert "only the first part" in parts[1] and parts[3] == {"inline_data": {"mime_type": "application/pdf", "data": "JVBERg=="}}
    assert summaries.user_parts(f, [])[-1] == "ATTACHED DOCUMENTS: none."


# --- GET /regulations/{id}/summary ---------------------------------------------

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
    def __init__(self, db, role="user", tenant="t1"):
        self.uid, self.role, self.tenant_id, self.business_unit_ids = "u-ravi", role, tenant, ["BU3"]
        self.tenant_ref = db.collection("tenants").document(tenant)


def seeded():
    db = FakeDb()
    t = ("tenants", "t1")
    db.docs[t + ("regulations", "r1")] = make_reg()
    db.docs[t + ("regulations", "r1", "activity", "a1")] = {k: v for k, v in ENTRIES[0].items() if k != "id"}
    db.docs[t + ("businessUnits", "BU2")] = {"label": "Computers"}
    db.docs[t + ("businessUnits", "BU3")] = {"label": "Data Centers"}
    db.docs[t + ("users", "u-ravi")] = {"displayName": "Ravi Menon", "email": "ravi@acme.example",
                                         "role": "user", "businessUnitIds": ["BU3"]}
    db.docs[t + ("regulations", "hidden1")] = {**make_reg(), "hidden": True}
    return db


@pytest.fixture
def api(monkeypatch):
    db = seeded()
    calls = []

    def fake_generate_json(tenant_id, feature, contents, **kw):
        calls.append({"tenant": tenant_id, "feature": feature, "contents": contents, **kw})
        if api.error:
            raise api.error
        return api.answer, AIResult(text="{}", model="gemini-3.1-flash-lite", input_tokens=900,
                                    output_tokens=300, cost_usd=0.0007, finish_reason="STOP")

    files = {uri("letter.docx"): _docx("Supplier letter: passport data ready for 1 of 3 models."),
             uri("survey.doc"): b"\xd0\xcf\x11\xe0"}
    monkeypatch.setattr(summary_routes, "_download", lambda t, u: files.get(u))
    monkeypatch.setattr(ai_client, "generate_json", fake_generate_json)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    app = FastAPI()
    app.include_router(summary_routes.router)
    app.dependency_overrides[get_current_context] = lambda: Ctx(db)
    api.client, api.db, api.calls, api.answer, api.error = TestClient(app), db, calls, GOOD, None
    return api


def test_first_open_generates_then_reuses(api):
    r = api.client.get("/regulations/r1/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["cached"] is False and body["aiGenerated"] is True and "legal advice" in body["disclaimer"]
    assert body["units"][0]["code"] == "BU3" and body["units"][0]["statusLabel"] == "red (Action needed)"
    assert body["units"][0]["contacts"] == [{"name": "Ravi Menon", "email": "ravi@acme.example"}]
    assert body["documents"] == {"read": ["letter.docx"], "partlyRead": [],
                                 "notRead": [{"fileName": "survey.doc", "reason": "older Word format (.doc)"}]}
    assert "could not be read: survey.doc" in body["documentsAndNotes"]

    call = api.calls[0]
    assert call["feature"] == "regulation_summary" and call["tenant"] == "t1" and call["actor_uid"] == "u-ravi"
    sent = json.dumps(call["contents"])
    assert "1 of 3 models" in sent and "@" not in sent

    again = api.client.get("/regulations/r1/summary").json()
    assert again["cached"] is True and len(api.calls) == 1
    assert again["short"] == body["short"] and again["generatedAt"] == body["generatedAt"]


def test_a_change_regenerates(api):
    api.client.get("/regulations/r1/summary")
    reg = api.db.docs[("tenants", "t1", "regulations", "r1")]
    reg["buStatuses"]["BU2"] = {"status": "pending", "note": "Re-checking docking stations."}
    assert api.client.get("/regulations/r1/summary").json()["cached"] is False and len(api.calls) == 2
    api.db.docs[("tenants", "t1", "regulations", "r1", "activity", "a9")] = {
        "buCode": "BU3", "actorUid": "u-ravi", "at": T0, "note": "New note", "removed": False}
    api.client.get("/regulations/r1/summary")
    assert len(api.calls) == 3


def test_contacts_are_live_not_cached(api):
    api.client.get("/regulations/r1/summary")
    api.db.docs[("tenants", "t1", "users", "u-new")] = {"displayName": "Ann New", "email": "ann@acme.example",
                                                         "role": "user", "businessUnitIds": ["BU3"]}
    body = api.client.get("/regulations/r1/summary").json()
    assert body["cached"] is True and [p["name"] for p in body["units"][0]["contacts"]] == ["Ann New", "Ravi Menon"]


def test_hidden_or_missing_regulation_is_404(api):
    assert api.client.get("/regulations/hidden1/summary").status_code == 404
    assert api.client.get("/regulations/nope/summary").status_code == 404
    assert api.calls == []


@pytest.mark.parametrize("error,status", [
    (ai_client.AILimitReached(), 429),
    (ai_client.AICreditsExhausted("402"), 503),
    (ai_client.AINotConfigured(), 503),
])
def test_ai_problems_show_their_message(api, error, status):
    api.error = error
    r = api.client.get("/regulations/r1/summary")
    assert r.status_code == status and r.json()["detail"] == error.message
    assert ("tenants", "t1", "aiSummaries", "r1") not in api.db.docs  # nothing cached


def test_unusable_answer_is_502_and_not_cached(api):
    api.answer = {"short": "", "regulation": ""}
    r = api.client.get("/regulations/r1/summary")
    assert r.status_code == 502 and r.json()["detail"] == ai_client.AINoAnswer.message
    assert ("tenants", "t1", "aiSummaries", "r1") not in api.db.docs


def test_document_budget(monkeypatch):
    monkeypatch.setattr(summaries, "MAX_DOCS", 1)
    reg = flagged(make_reg())
    f = summaries.build_facts(reg, [], LABELS, PEOPLE)
    monkeypatch.setattr(summary_routes, "_download", lambda t, u: _docx("hello world"))
    reads, report = summary_routes.read_documents("t1", reg, f)
    assert len(reads) == 1 and report["notRead"][0]["reason"] == "only the first 1 documents are read"


def test_download_refuses_other_companies_files():
    assert summary_routes._download("t1", "gs://regulator-dev-documents/tenants/t2/x.pdf") is None
    assert summary_routes._download("t1", "gs://regulator-dev-documents/tenants/t1/../t2/x.pdf") is None
    assert summary_routes._download("t1", "") is None
