"""
T065 - Business Unit activity (Srinivas, Oct 29). Pure logic, no server:
what one save records, who may remove a note, and how the history is built
(including records from before v0.7.0).
"""
from datetime import datetime, timedelta, timezone

from core.activity import FORMER_USER, build_activity, can_remove, latest_note, make_entry

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
PEOPLE = {
    "u-priya": {"displayName": "Priya Shah", "email": "priya@acme.example"},
    "u-dan": {"displayName": "Daniel Ortiz", "email": "dan@acme.example"},
}


def entry(**kw):
    base = dict(bu_code="BU1", actor_uid="u-priya", actor_role="user", now=T0, previous_status=None,
                status_sent=False, new_status=None, note=None, documents=[])
    base.update(kw)
    return make_entry(**base)


# --- make_entry ------------------------------------------------------------------

def test_nothing_to_record_returns_none():
    assert entry() is None
    assert entry(status_sent=True, previous_status="pending", new_status="pending") is None
    assert entry(note="   ") is None


def test_status_change_records_before_and_after():
    e = entry(status_sent=True, previous_status="pending", new_status="action_needed")
    assert e["statusChanged"] and e["status"] == "action_needed" and e["previousStatus"] == "pending"
    assert e["removed"] is False and e["actorUid"] == "u-priya" and e["at"] == T0


def test_setting_not_set_is_a_change():
    e = entry(status_sent=True, previous_status="compliant", new_status=None)
    assert e["statusChanged"] and e["status"] is None


def test_note_and_documents_saved_together():
    doc = {"key": "k1", "fileName": "UN38.3.pdf", "sizeBytes": 2048, "gcsUri": "gs://b/x", "uploadedBy": "u-priya"}
    e = entry(note=" Waiting on supplier ", documents=[doc])
    assert e["note"] == "Waiting on supplier" and not e["statusChanged"]
    assert e["documents"] == [{"key": "k1", "fileName": "UN38.3.pdf", "sizeBytes": 2048}]


# --- can_remove --------------------------------------------------------------------

def test_only_your_own_plain_note_can_be_removed():
    note = {**entry(note="hello"), "id": "a"}
    assert can_remove(note, "u-priya")
    assert not can_remove(note, "u-dan")
    assert not can_remove({**note, "removed": True}, "u-priya")
    assert not can_remove({**note, "statusChanged": True}, "u-priya")
    assert not can_remove({**note, "documents": [{"key": "k"}]}, "u-priya")
    assert not can_remove({**note, "legacy": True}, "u-priya")


# --- build_activity ------------------------------------------------------------------

def reg(**kw):
    base = {"businessUnitIds": ["BU1", "BU2"], "documents": [], "buStatuses": {}}
    base.update(kw)
    return base


def test_newest_first_with_names_and_removed_left_out():
    entries = [
        {**entry(note="first"), "id": "a"},
        {**entry(note="second", now=T0 + timedelta(days=1), actor_uid="u-dan"), "id": "b"},
        {**entry(note="gone", now=T0 + timedelta(days=2)), "id": "c", "removed": True},
    ]
    out = build_activity(reg(), entries, PEOPLE, "u-priya")
    assert [e["id"] for e in out] == ["b", "a"]
    assert out[0]["actorName"] == "Daniel Ortiz" and out[0]["actorEmail"] == "dan@acme.example"
    assert out[0]["canRemove"] is False and out[1]["canRemove"] is True
    assert "actorRole" not in out[0]


def test_unknown_person_shows_as_former_user():
    out = build_activity(reg(), [{**entry(note="x", actor_uid="u-gone"), "id": "a"}], PEOPLE, "u-priya")
    assert out[0]["actorName"] == FORMER_USER and out[0]["actorEmail"] == ""


def test_filter_by_unit():
    entries = [{**entry(note="one"), "id": "a"}, {**entry(note="two", bu_code="BU2"), "id": "b"}]
    assert [e["id"] for e in build_activity(reg(), entries, PEOPLE, "u-priya", "BU2")] == ["b"]


def test_hidden_document_dropped_from_its_entry_but_entry_kept():
    docs = [{"key": "k1", "fileName": "a.pdf", "buCode": "BU1", "hidden": True},
            {"key": "k2", "fileName": "b.pdf", "buCode": "BU1", "hidden": False, "uploadedBy": "u-priya"}]
    e = {**entry(note="two files", documents=[docs[0], docs[1]]), "id": "a"}
    out = build_activity(reg(documents=docs), [e], PEOPLE, "u-priya")
    assert len(out) == 1 and [d["key"] for d in out[0]["documents"]] == ["k2"]


def test_records_from_before_v070_are_shown():
    docs = [{"key": "k9", "fileName": "old.pdf", "buCode": "BU2", "hidden": False,
             "uploadedBy": "u-dan", "uploadedAt": T0 - timedelta(days=3)},
            {"key": "kr", "fileName": "regwide.pdf", "hidden": False}]  # regulation-wide: not a unit entry
    statuses = {"BU1": {"status": "pending", "note": "old note", "updatedBy": "u-priya", "updatedAt": T0}}
    out = build_activity(reg(documents=docs, buStatuses=statuses), [], PEOPLE, "u-priya")
    assert [e["id"] for e in out] == ["status-BU1", "doc-k9"]
    assert out[0]["legacy"] and out[0]["status"] == "pending" and out[0]["note"] == "old note"
    assert out[0]["canRemove"] is False
    assert out[1]["actorName"] == "Daniel Ortiz" and out[1]["documents"][0]["fileName"] == "old.pdf"


def test_legacy_status_hidden_once_the_unit_has_its_own_entries():
    statuses = {"BU1": {"status": "pending", "note": "old", "updatedBy": "u-priya", "updatedAt": T0}}
    e = {**entry(note="new", now=T0 + timedelta(hours=1)), "id": "a"}
    out = build_activity(reg(buStatuses=statuses), [e], PEOPLE, "u-priya")
    assert [x["id"] for x in out] == ["a"]


def test_linked_document_not_shown_twice():
    docs = [{"key": "k1", "fileName": "a.pdf", "buCode": "BU1", "hidden": False, "uploadedAt": T0}]
    e = {**entry(note="with file", documents=docs), "id": "a"}
    out = build_activity(reg(documents=docs), [e], PEOPLE, "u-priya")
    assert [x["id"] for x in out] == ["a"]


# --- latest_note ----------------------------------------------------------------------

def test_latest_note_after_removing_one():
    entries = [
        {**entry(note="older"), "id": "a"},
        {**entry(note="newer", now=T0 + timedelta(days=1)), "id": "b"},
        {**entry(status_sent=True, new_status="pending", now=T0 + timedelta(days=2)), "id": "c"},
    ]
    assert latest_note(entries, "BU1", skip_id="b") == "older"
    assert latest_note(entries, "BU1") == "newer"
    assert latest_note(entries, "BU2") == ""
