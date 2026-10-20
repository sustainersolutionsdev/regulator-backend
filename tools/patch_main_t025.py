"""
One-off patch of main.py for T025. Run once from regulator-backend:

    python tools\\patch_main_t025.py            (shows what will change)
    python tools\\patch_main_t025.py --apply    (writes main.py)

Three edits, each matched exactly. If any anchor is missing or appears more
than once, it stops without writing anything:
  1. imports BackgroundTasks from fastapi;
  2. POST /regulations takes a BackgroundTasks parameter;
  3. POST /regulations hands the new regulation to the email alert
     (core/alerts.py, FR-3.1) just before it returns, so the email is sent
     after the response and never slows down or fails the save.

Line endings (CRLF/LF) are preserved; no BOM is written.
Safe to re-run: an already-patched file is detected and left alone.
"""
import sys
from pathlib import Path

MAIN = Path(__file__).resolve().parent.parent / "main.py"
MARKER = "# T025 (FR-3.1, DL-9): email alert"

OLD_IMPORT = "from fastapi import FastAPI, Depends, HTTPException, UploadFile, File"
NEW_IMPORT = "from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, BackgroundTasks"
OLD_PARAM = "    payload: AddRegulationRequest,"
NEW_PARAM_LINES = ["    payload: AddRegulationRequest,", "    background_tasks: BackgroundTasks,"]
OLD_RETURN = "    return record"


def main() -> int:
    raw = MAIN.read_bytes()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    nl = "\r\n" if "\r\n" in text else "\n"

    if MARKER in text:
        print("main.py is already patched for T025. Nothing to do.")
        return 0

    # "    return record" must be the one at the end of add_regulation: it is
    # followed by the blank lines and the GET /regulations decorator.
    return_anchor = OLD_RETURN + nl + nl + nl + '@app.get("/regulations")'
    checks = {
        "fastapi import line": (text.count(OLD_IMPORT + nl), OLD_IMPORT),
        "add_regulation payload parameter": (text.count(OLD_PARAM + nl), OLD_PARAM),
        "add_regulation 'return record'": (text.count(return_anchor), return_anchor),
    }
    problems = [f"{name}: expected 1 match, found {n}" for name, (n, _) in checks.items() if n != 1]
    if problems:
        print("NOT PATCHED - main.py does not look as expected:")
        for p in problems:
            print("  -", p)
        return 1

    new = text.replace(OLD_IMPORT + nl, NEW_IMPORT + nl, 1)
    new = new.replace(OLD_PARAM + nl, nl.join(NEW_PARAM_LINES) + nl, 1)
    new = new.replace(
        return_anchor,
        nl.join([
            "    " + MARKER + ", sent after the response;",
            "    # skipped when the regulation is not on the Roadmap/Dashboard.",
            "    from core.alerts import schedule_regulation_added_alert",
            '    schedule_regulation_added_alert(background_tasks, ctx.tenant_id, record["id"], ctx.uid)',
            OLD_RETURN,
            "",
            "",
            '@app.get("/regulations")',
        ]),
        1,
    )

    print("1. Import BackgroundTasks from fastapi")
    print("2. POST /regulations: add background_tasks parameter")
    print("3. POST /regulations: schedule the new-regulation email alert before returning")

    if "--apply" not in sys.argv:
        print("\nDry run only. Re-run with --apply to write main.py.")
        return 0

    MAIN.write_bytes(new.encode("utf-8"))
    if had_bom:
        print("Note: the UTF-8 BOM that was at the start of main.py has been removed.")
    print("main.py patched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
