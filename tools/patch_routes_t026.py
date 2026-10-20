"""
One-off patch of core/regulation_routes.py for T026. Run once from
regulator-backend:

    python tools\\patch_routes_t026.py            (shows what will change)
    python tools\\patch_routes_t026.py --apply    (writes the file)

Three edits, each matched exactly; stops without writing if any anchor is
missing or appears more than once:
  1. imports BackgroundTasks from fastapi;
  2. PATCH /regulations/{id} takes a BackgroundTasks parameter;
  3. just before PATCH returns, hands the edit to the email alert
     (core/alerts.py schedule_regulation_updated_alert, FR-3.2, DL-9): an
     "updated" email when a main detail really changed, the "New regulation"
     email when "Add to Roadmap and Dashboard" went from No to Yes, nothing
     otherwise. Sent after the response, so it never slows or fails the edit.

Status, explanations and documents use other endpoints and never email
(DL-9, 1A). Line endings are preserved; safe to re-run.
"""
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "core" / "regulation_routes.py"
MARKER = "# T026 (FR-3.2, DL-9): email alert"

OLD_IMPORT = "from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile"
NEW_IMPORT = "from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile"
OLD_PARAM = "    payload: UpdateRegulationRequest,"
NEW_PARAM = ["    payload: UpdateRegulationRequest,", "    background_tasks: BackgroundTasks,"]
OLD_RETURN = '    return with_document_flags({"id": reg_id, **current, **update})'


def main() -> int:
    raw = TARGET.read_bytes()
    text = raw.decode("utf-8-sig")
    nl = "\r\n" if "\r\n" in text else "\n"

    if MARKER in text:
        print("regulation_routes.py is already patched for T026. Nothing to do.")
        return 0

    anchors = {
        "fastapi import line": OLD_IMPORT + nl,
        "update_regulation payload parameter": OLD_PARAM + nl,
        "update_regulation return": OLD_RETURN + nl,
    }
    problems = [f"{k}: expected 1 match, found {text.count(v)}" for k, v in anchors.items() if text.count(v) != 1]
    if problems:
        print("NOT PATCHED - regulation_routes.py does not look as expected:")
        for p in problems:
            print("  -", p)
        return 1

    new = text.replace(OLD_IMPORT + nl, NEW_IMPORT + nl, 1)
    new = new.replace(OLD_PARAM + nl, nl.join(NEW_PARAM) + nl, 1)
    new = new.replace(
        OLD_RETURN + nl,
        nl.join([
            "    " + MARKER + " after the response: main-detail changes only;",
            '    # "Add to Roadmap and Dashboard" No -> Yes sends the "New regulation" email.',
            "    from core.alerts import schedule_regulation_updated_alert",
            "    schedule_regulation_updated_alert(background_tasks, ctx.tenant_id, reg_id, ctx.uid, current, update)",
            OLD_RETURN,
        ]) + nl,
        1,
    )

    print("1. Import BackgroundTasks from fastapi")
    print("2. PATCH /regulations/{id}: add background_tasks parameter")
    print("3. PATCH /regulations/{id}: schedule the updated-regulation alert before returning")
    if "--apply" not in sys.argv:
        print("\nDry run only. Re-run with --apply to write the file.")
        return 0
    TARGET.write_bytes(new.encode("utf-8"))
    print("core/regulation_routes.py patched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
