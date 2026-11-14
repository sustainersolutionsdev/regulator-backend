"""T076/T077: numbers behind the Insights charts and the AI notes on them (pure, no network)."""
import json

import pytest

from core import chart_notes as cn

UNITS = {"BU1": "Printers", "BU2": "Data Centers"}
F = {"q": "", "domain": [], "bu": [], "country": [], "region": [], "dueFrom": "", "dueTo": "", "docs": ""}


def reg(rid, domain, country, due, statuses, explanation="secret explanation"):
    return {"id": rid, "domain": domain, "country": country, "region": "", "dueDate": due,
            "businessUnitIds": list(statuses),
            "buStatuses": {bu: {"status": st, "explanation": explanation} for bu, st in statuses.items() if st}}


REGS = [
    reg("r1", "Batteries & EVs", "US", "2026-02-10", {"BU1": "action_needed", "BU2": "compliant"}),
    reg("r2", "Batteries & EVs", "EU", "2026-11-30", {"BU1": "pending"}),
    reg("r3", "Chemicals", "", "", {"BU2": None}),
]


def test_quarter_of():
    assert cn.quarter_of("2026-02-10") == "2026 Q1"
    assert cn.quarter_of("2026-10-01") == "2026 Q4"
    assert cn.quarter_of("") == cn.NO_DUE_DATE


def test_chart_data_counts_regulations_once_and_statuses_per_unit():
    d = cn.chart_data(REGS, F, UNITS)
    assert d["totals"] == {"regulations": 3, "businessUnitStatuses": 4}
    assert d["domain"][0] == {"label": "Batteries & EVs", "count": 2}
    assert {"label": "Not specified", "count": 1} in d["country"]
    assert d["bu"] == [{"label": "Data Centers", "count": 2}, {"label": "Printers", "count": 2}]
    assert [q["label"] for q in d["quarter"]] == ["2026 Q1", "2026 Q4", cn.NO_DUE_DATE]
    q1 = d["quarter"][0]
    assert (q1["action_needed"], q1["compliant"], q1["pending"], q1["not_set"]) == (1, 1, 0, 0)
    grid = {g["label"]: g for g in d["grid"]}
    assert grid["Data Centers"]["not_set"] == 1 and grid["Printers"]["pending"] == 1


def test_business_unit_filter_counts_only_selected_units():
    d = cn.chart_data(REGS[:2], {**F, "bu": ["BU1"]}, UNITS)
    assert d["totals"]["businessUnitStatuses"] == 2
    assert [g["label"] for g in d["grid"]] == ["Printers"]


def test_prompt_holds_numbers_only():
    prompt = cn.user_prompt(cn.chart_data(REGS, F, UNITS), "due dates Jan 2026 - Dec 2026")
    assert "secret explanation" not in prompt and "BU1" not in prompt
    data = json.loads(prompt.split("DATA:\n", 1)[1])
    assert set(data["charts"]) == {"domain", "region", "country", "bu", "quarter", "grid"}
    assert data["charts"]["grid"]["rows"][0].keys() >= {"Action needed", "Compliant", "Pending", "Not set"}


def test_long_charts_fold_into_other():
    regs = [reg(f"r{i}", f"Domain {i:02d}", "US", "2026-03-01", {"BU1": "pending"}) for i in range(40)]
    data = json.loads(cn.user_prompt(cn.chart_data(regs, F, UNITS), "v").split("DATA:\n", 1)[1])
    rows = data["charts"]["domain"]["rows"]
    assert len(rows) == cn.MAX_ROWS and rows[-1]["label"].startswith("Other (") and sum(r["count"] for r in rows) == 40


def test_parse_output_two_sentences_scrubbed_and_only_charts_with_data():
    d = cn.chart_data(REGS, F, UNITS)
    d["region"] = []  # pretend this chart is empty
    answer = {"domain": "Batteries & EVs leads with 2. Chemicals has 1. A third sentence is dropped.",
              "region": "should be ignored", "bu": "BU1 has 2; mail a@b.com.", "quarter": 7}
    notes = cn.parse_output(answer, d, UNITS)
    assert notes["domain"] == "Batteries & EVs leads with 2. Chemicals has 1."
    assert "region" not in notes and "quarter" not in notes
    assert notes["bu"] == "Printers has 2; mail."


def test_parse_output_rejects_empty_answers():
    d = cn.chart_data(REGS, F, UNITS)
    with pytest.raises(cn.InvalidNotes):
        cn.parse_output({}, d, UNITS)
    with pytest.raises(cn.InvalidNotes):
        cn.parse_output(["not", "an", "object"], d, UNITS)


def test_fingerprint_changes_with_numbers_view_and_model():
    d = cn.chart_data(REGS, F, UNITS)
    base = cn.fingerprint(d, "v", "m")
    assert base == cn.fingerprint(cn.chart_data(REGS, F, UNITS), "v", "m")
    assert base != cn.fingerprint(cn.chart_data(REGS[:2], F, UNITS), "v", "m")
    assert base != cn.fingerprint(d, "other view", "m") != cn.fingerprint(d, "v", "m2")


def test_empty_view_has_no_data():
    assert not cn.has_data(cn.chart_data([], F, UNITS))
