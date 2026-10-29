"""
core/activity.py - Business Unit activity on the Dashboard (T065).

Approved by Srinivas, Oct 29 2026 ("go with the mockup"), from his Oct 28
suggestions and Niranjan's Oct 27 notes:
  - Saving in a unit's pop-up records ONE activity entry: the status change
    (if any), the note (if any) and the documents attached with it, with the
    person and the time. Everyone in the company sees the history, newest
    first, so they know whom to contact.
  - Notes can't be edited. A person may remove only their own note; removal
    is soft (the entry stays, marked removed, NFR-8). Entries that changed
    the status or added a document can't be removed, so the record of what
    happened stays complete.

Stored: tenants/{t}/regulations/{id}/activity/{auto-id}
  {buCode, actorUid, actorRole, at, statusChanged, status, previousStatus,
   note, documents: [{key, fileName, sizeBytes}], removed,
   removedAt?, removedBy?}

Records from before v0.7.0 have no entries. build_activity() fills the gap
from what each regulation already holds - the unit's current status / note
(buStatuses: updatedBy, updatedAt) and each unit document (uploadedBy,
uploadedAt) - marked "legacy", so the history is never blank.

The functions here are pure (no Firestore), so tests need no server.
"""
from datetime import datetime, timezone
from typing import Optional

FORMER_USER = "Former user"


def _when(value) -> datetime:
    """Sort key: Firestore timestamps, datetimes or ISO strings; unknown = oldest."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def doc_summary(doc: dict) -> dict:
    return {"key": doc["key"], "fileName": doc.get("fileName") or "document", "sizeBytes": doc.get("sizeBytes")}


def make_entry(*, bu_code: str, actor_uid: str, actor_role: str, now, previous_status, status_sent: bool,
               new_status, note: Optional[str], documents: list[dict]) -> Optional[dict]:
    """
    The activity entry for one save, or None when there is nothing to record
    (same status, no note, no document).
    """
    status_changed = status_sent and new_status != previous_status
    note = (note or "").strip()
    if not status_changed and not note and not documents:
        return None
    return {
        "buCode": bu_code,
        "actorUid": actor_uid,
        "actorRole": actor_role,
        "at": now,
        "statusChanged": status_changed,
        "status": new_status if status_changed else None,
        "previousStatus": previous_status if status_changed else None,
        "note": note,
        "documents": [doc_summary(d) for d in documents],
        "removed": False,
    }


def can_remove(entry: dict, uid: str) -> bool:
    """Your own note only; never an entry that changed the status or added a document."""
    return (
        not entry.get("legacy")
        and not entry.get("removed")
        and entry.get("actorUid") == uid
        and not entry.get("statusChanged")
        and not entry.get("documents")
        and bool(entry.get("note"))
    )


def build_activity(reg: dict, entries: list[dict], people: dict, uid: str,
                   bu_code: Optional[str] = None) -> list[dict]:
    """
    The history shown in the pop-ups, newest first.

    reg      the regulation, with document `key` / `hidden` flags (with_document_flags)
    entries  stored activity entries, each with its `id`
    people   uid -> {displayName, email} for the caller's company
    """
    units = list(reg.get("businessUnitIds") or [])
    if bu_code is not None:
        units = [u for u in units if u == bu_code]
    docs = {d["key"]: d for d in reg.get("documents") or []}

    live = [dict(e) for e in entries if not e.get("removed") and e.get("buCode") in units]
    linked = {d.get("key") for e in live for d in e.get("documents") or []}
    with_status = {e["buCode"] for e in live if e.get("statusChanged") or e.get("note")}

    out = []
    for e in live:
        e["documents"] = [
            {**doc_summary(docs[d["key"]]), "uploadedBy": docs[d["key"]].get("uploadedBy")}
            for d in e.get("documents") or []
            if d.get("key") in docs and not docs[d["key"]].get("hidden")
        ]
        e["legacy"] = False
        out.append(e)

    # Unit documents uploaded before v0.7.0 (or from the Roadmap), not part of any entry.
    for key, d in docs.items():
        if d.get("hidden") or key in linked or d.get("buCode") not in units:
            continue
        out.append({
            "id": f"doc-{key}", "buCode": d["buCode"], "actorUid": d.get("uploadedBy"), "at": d.get("uploadedAt"),
            "statusChanged": False, "status": None, "previousStatus": None, "note": "",
            "documents": [{**doc_summary(d), "uploadedBy": d.get("uploadedBy")}], "removed": False, "legacy": True,
        })

    # A unit's status / note set before v0.7.0: shown as one entry until the unit has its own.
    for bu in units:
        if bu in with_status:
            continue
        cur = (reg.get("buStatuses") or {}).get(bu) or {}
        if cur.get("status") is None and not (cur.get("note") or "").strip():
            continue
        out.append({
            "id": f"status-{bu}", "buCode": bu, "actorUid": cur.get("updatedBy"),
            "at": cur.get("updatedAt"), "statusChanged": cur.get("status") is not None,
            "status": cur.get("status"), "previousStatus": None, "note": (cur.get("note") or "").strip(),
            "documents": [], "removed": False, "legacy": True,
        })

    for e in out:
        person = people.get(e.get("actorUid")) or {}
        e["actorName"] = person.get("displayName") or person.get("email") or FORMER_USER
        e["actorEmail"] = person.get("email") or ""
        e["canRemove"] = can_remove(e, uid)
        e.pop("actorRole", None)
    out.sort(key=lambda e: _when(e.get("at")), reverse=True)
    return out


def latest_note(entries: list[dict], bu_code: str, skip_id: Optional[str] = None) -> str:
    """The newest remaining note for a unit, after one is removed ("" if none)."""
    notes = [e for e in entries
             if e.get("buCode") == bu_code and not e.get("removed") and e.get("id") != skip_id and e.get("note")]
    notes.sort(key=lambda e: _when(e.get("at")), reverse=True)
    return notes[0]["note"] if notes else ""
