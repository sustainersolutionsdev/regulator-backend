"""
One-off patch of main.py for T020. Run once from regulator-backend:

    python tools\\patch_main_t020.py            (shows what will change)
    python tools\\patch_main_t020.py --apply    (writes main.py)

It makes three edits, each matched exactly; if any anchor is missing or
appears more than once it stops without writing anything:
  1. removes the test-only POST /test-write/{bu_code} endpoint (T009 note:
     replaced by the real Business Unit-scoped write, PATCH
     /regulations/{id}/bu-status/{bu_code});
  2. GET /regulations returns each document with its `key` and `hidden`
     flag (with_document_flags, core/regulation_routes.py);
  3. mounts the saved-Dashboard-view routes (core/preference_routes.py).

Line endings (CRLF/LF) and encoding are preserved; no BOM is written.
Safe to re-run: an already-patched file is detected and left alone.
"""
import re
import sys
from pathlib import Path

MAIN = Path(__file__).resolve().parent.parent / "main.py"
MARKER = "# T020: saved Dashboard view routes"

TEST_WRITE_RE = re.compile(
    r'@app\.post\("/test-write/\{bu_code\}"\)\r?\n'
    r"def test_write\(bu_code: str, ctx: RequestContext = Depends\(get_current_context\)\):\r?\n"
    r"(?:[ \t]+[^\r\n]*\r?\n)+?"
    r"[ \t]+return \{\"message\": f\"Write to '\{bu_code\}' allowed for role '\{ctx\.role\}'\.\"\}\r?\n"
    r"(?:[ \t]*\r?\n)*"
)
OLD_RETURN = "        return list_regulations(ctx.tenant_id)"
NEW_RETURN = "        return [with_document_flags(r) for r in list_regulations(ctx.tenant_id)]"
ROUTER_ANCHOR = "app.include_router(regulation_edit_router)"


def main() -> int:
    raw = MAIN.read_bytes()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    nl = "\r\n" if "\r\n" in text else "\n"

    if MARKER in text:
        print("main.py is already patched for T020. Nothing to do.")
        return 0

    problems = []
    matches = TEST_WRITE_RE.findall(text)
    if len(matches) != 1:
        problems.append(f"/test-write endpoint: expected 1 match, found {len(matches)}")
    if text.count(OLD_RETURN) != 1:
        problems.append(f"GET /regulations return line: expected 1 match, found {text.count(OLD_RETURN)}")
    if text.count(ROUTER_ANCHOR) != 1:
        problems.append(f"'{ROUTER_ANCHOR}': expected 1 match, found {text.count(ROUTER_ANCHOR)}")
    if problems:
        print("NOT PATCHED - main.py does not look as expected:")
        for p in problems:
            print("  -", p)
        return 1

    new = TEST_WRITE_RE.sub("", text, count=1)
    new = new.replace(OLD_RETURN, NEW_RETURN, 1)
    addition = nl.join([
        ROUTER_ANCHOR,
        "",
        MARKER + " (core/preference_routes.py); GET /regulations",
        "# document flags (core/regulation_routes.py).",
        "from core.regulation_routes import with_document_flags",
        "from core.preference_routes import router as preference_router",
        "app.include_router(preference_router)",
    ])
    new = new.replace(ROUTER_ANCHOR, addition, 1)

    removed = matches[0].count("\n")
    print(f"1. Remove /test-write endpoint ({removed} lines)")
    print("2. GET /regulations: add document key/hidden flags")
    print("3. Mount preference routes after the T014 router")

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
