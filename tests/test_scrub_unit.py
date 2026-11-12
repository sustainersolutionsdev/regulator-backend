"""T033: rules kept by code on AI text (no email addresses, unit names not codes)."""

from core import insights, summaries
from core.summaries import scrub

NAMES = {"BU1": "Printers", "BU2": "Data Centers", "Default": "Default"}


def test_scrub_removes_email_addresses():
    out = scrub("Contact Ravi (ravi.menon@acme.example) for the passport data.", NAMES)
    assert "@" not in out
    assert out == "Contact Ravi for the passport data."


def test_scrub_replaces_unit_codes_with_names():
    assert scrub("BU1 is red (Action needed); BU2 is green (Compliant).", NAMES) == \
        "Printers is red (Action needed); Data Centers is green (Compliant)."


def test_scrub_leaves_codes_inside_other_words_and_same_name_units():
    assert scrub("See BU10 and the Default unit; XBU1 stays.", NAMES) == "See BU10 and the Default unit; XBU1 stays."


def test_scrub_handles_empty():
    assert scrub("", NAMES) == ""


def test_regulation_summary_output_is_scrubbed():
    facts = {"units": [{"code": "BU1", "name": "Printers", "status": "action_needed",
                        "statusLabel": "red (Action needed)", "explanation": "x"}]}
    data = {"short": "BU1 is red (Action needed). Ask a@b.com.", "regulation": "Applies to BU1.",
            "documentsAndNotes": "None.", "units": [{"code": "BU1", "summary": "BU1 waits on a@b.com."}],
            "actionsNeeded": [{"code": "BU1", "action": "BU1 to send data to a@b.com."}]}
    out = summaries.parse_output(data, facts, [])
    text = json_text(out)
    assert "BU1" not in text.replace('"BU1"', "") and "@" not in text
    assert out["short"].startswith("Printers is red")


def test_insights_output_is_scrubbed():
    kept = [{"id": "e1", "unitCode": "BU2", "unit": "Data Centers", "status": "pending", "explanation": ""}]
    data = {"short": "BU2 is amber (Pending).", "overview": "Email x@y.org.", "entries": [{"id": "e1", "text": "BU2 waits."}]}
    out = insights.parse_output(data, kept)
    assert out["short"] == "Data Centers is amber (Pending)."
    assert "@" not in out["overview"]
    assert out["entries"][0]["text"] == "Data Centers waits."


def json_text(out):
    import json
    return json.dumps(out)
