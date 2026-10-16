"""
T020 one-off data migration (DL-8): single regulation status -> per Business Unit.

T018 stored one `status` / `statusNote` per regulation. DL-8 (Srinivas,
Oct 16 2026) keeps status per Business Unit instead (`buStatuses`). This
copies each regulation's single status and explanation to EVERY Business
Unit it is tagged to, then clears the old fields (set to null, not deleted,
so the change is visible in the audit log like any other write).

Run once from regulator-backend, after T020 is deployed locally:
    python scripts\\migrate_t020_bu_status.py            (dry run: lists only)
    python scripts\\migrate_t020_bu_status.py --apply    (writes)

  - Only regulator-dev; refuses any other project.
  - Skips a regulation that already has an entry in buStatuses for a
    Business Unit (never overwrites a per-unit status set after T020).
  - Every write goes through write_with_audit() (action "migrate_bu_status").
  - Safe to re-run: a migrated regulation has no old status left to copy.
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from datetime import datetime, timezone

from core.audit import write_with_audit
from core.tenancy import db

ALLOWED_PROJECT = "regulator-dev"
ACTOR = "migration:t020"


def plan_for(data: dict):
    """Returns the write for one regulation, or None if nothing to migrate."""
    old_status = data.get("status")
    old_note = (data.get("statusNote") or "").strip()
    if old_status is None and not old_note:
        return None

    existing = data.get("buStatuses") or {}
    now = datetime.now(timezone.utc)
    entries = {}
    for bu in data.get("businessUnitIds") or []:
        if bu in existing:
            continue
        entries[bu] = {
            "status": old_status,
            "note": old_note,
            "updatedAt": now,
            "updatedBy": ACTOR,
            "statusChangedAt": data.get("statusUpdatedAt") or now,
        }
    return {"buStatuses": entries, "status": None, "statusNote": None}


def run(apply: bool) -> int:
    if db.project != ALLOWED_PROJECT:
        print(f"Refusing to run against project '{db.project}' (only '{ALLOWED_PROJECT}').")
        return 1

    total = 0
    for tenant in db.collection("tenants").stream():
        for doc in tenant.reference.collection("regulations").stream():
            data = doc.to_dict()
            plan = plan_for(data)
            if plan is None:
                continue
            total += 1
            units = ", ".join(plan["buStatuses"]) or "(no unit without a status already)"
            print(f"[{tenant.id}] {data.get('title', doc.id)[:60]}: "
                  f"status={data.get('status')!r} -> {units}")
            if apply:
                write_with_audit(
                    tenant_id=tenant.id,
                    doc_ref=doc.reference,
                    data=plan,
                    actor_uid=ACTOR,
                    actor_role="system",
                    action="migrate_bu_status",
                )

    verb = "migrated" if apply else "would migrate"
    print(f"\n{verb}: {total} regulation(s).")
    if not apply and total:
        print("Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(run("--apply" in sys.argv))
