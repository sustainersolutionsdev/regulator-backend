"""
One-off patch of tests/cleanup_test_data.py for T022. Run once from
regulator-backend:

    python tools\\patch_cleanup_t022.py            (shows what will change)
    python tools\\patch_cleanup_t022.py --apply    (writes the file)

It makes four edits, each matched exactly; if any anchor is missing or
appears more than once it stops without writing anything:
  1. registers the "T022 QC Regulation " title prefix
     (tests/test_m2_qc_hardening.py);
  2. adds _orphan_test_logins(): test logins (the test_users.py email
     pattern) that have no directory record in any test tenant - what a
     sign-up that failed half-way leaves behind (seen Oct 16, 2026). The
     existing pass never finds them, because it starts from the directory
     records;
  3. resolves the test tenants once and reuses them;
  4. reports and (with --apply) deletes those orphan logins, counted as
     "orphan_logins".

Line endings (CRLF/LF) are preserved; no BOM is written. Safe to re-run:
an already-patched file is detected and left alone.
"""
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "tests" / "cleanup_test_data.py"
MARKER = '"T022 QC Regulation "'

PREFIX_ANCHOR = '    "T015 Perf Regulation ",'
TOTALS_OLD = 'totals = {"regulations": 0, "business_units": 0, "users": 0, "files": 0}'
TOTALS_NEW = 'totals = {"regulations": 0, "business_units": 0, "users": 0, "files": 0, "orphan_logins": 0}'
LOOP_OLD = "    for tenant_id in _test_tenant_ids():"
RUN_ANCHOR = "def run_cleanup("
RETURN_ANCHOR = "    return totals"

ORPHAN_FUNC = [
    "def _orphan_test_logins(tenant_ids: list[str]) -> list:",
    '    """',
    "    T022: test logins with no directory record in any test tenant - left",
    "    by a sign-up that failed half-way (e.g. a dropped connection to",
    "    Firebase). Same email pattern and protected accounts as _is_test_user;",
    "    a login whose claims name a tenant outside the test tenants is never",
    "    touched.",
    '    """',
    "    orphans = []",
    "    for user in auth.list_users().iterate_all():",
    '        email = (user.email or "").lower()',
    "        if email in PROTECTED_EMAILS or not TEST_USER_EMAIL.match(email):",
    "            continue",
    '        claimed = (user.custom_claims or {}).get("tenant_id")',
    "        if claimed and claimed not in tenant_ids:",
    "            continue",
    "        if any(",
    '            db.collection("tenants").document(t).collection("users").document(user.uid).get().exists',
    "            for t in tenant_ids",
    "        ):",
    "            continue",
    "        orphans.append(user)",
    "    return orphans",
    "",
    "",
]

ORPHAN_STEP = [
    "    # T022: logins left with no directory record (see _orphan_test_logins).",
    "    orphans = _orphan_test_logins(tenant_ids)",
    "    if verbose:",
    "        print(",
    '            f"[cleanup] orphan test logins: {len(orphans)}"',
    '            + ("" if apply else "  (dry run - nothing deleted)")',
    "        )",
    "        if not apply:",
    "            for user in orphans:",
    '                print(f"           login {user.email}")',
    "    if apply:",
    "        for user in orphans:",
    "            try:",
    "                auth.delete_user(user.uid)",
    "            except auth.UserNotFoundError:",
    "                pass",
    '    totals["orphan_logins"] += len(orphans)',
    "",
]


def main() -> int:
    raw = TARGET.read_bytes()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    nl = "\r\n" if "\r\n" in text else "\n"

    if MARKER in text:
        print("cleanup_test_data.py is already patched for T022. Nothing to do.")
        return 0

    problems = []
    for label, anchor in [
        ("T015 prefix line", PREFIX_ANCHOR),
        ("totals line", TOTALS_OLD),
        ("tenant loop", LOOP_OLD),
        ("run_cleanup definition", RUN_ANCHOR),
        ("return totals", RETURN_ANCHOR),
    ]:
        n = text.count(anchor)
        if n != 1:
            problems.append(f"{label}: expected 1 match, found {n}")
    if problems:
        print("NOT PATCHED - cleanup_test_data.py does not look as expected:")
        for p in problems:
            print("  -", p)
        return 1

    new = text.replace(
        PREFIX_ANCHOR,
        '    "T022 QC Regulation ",       # test_m2_qc_hardening.py' + nl + PREFIX_ANCHOR,
        1,
    )
    new = new.replace(TOTALS_OLD, TOTALS_NEW, 1)
    new = new.replace(LOOP_OLD, "    tenant_ids = _test_tenant_ids()" + nl + "    for tenant_id in tenant_ids:", 1)
    new = new.replace(RUN_ANCHOR, nl.join(ORPHAN_FUNC) + RUN_ANCHOR, 1)
    new = new.replace(RETURN_ANCHOR, nl.join(ORPHAN_STEP) + RETURN_ANCHOR, 1)

    print('1. Register title prefix "T022 QC Regulation "')
    print("2. Add _orphan_test_logins()")
    print("3. Resolve the test tenants once")
    print("4. Report / delete orphan test logins (totals: orphan_logins)")

    if "--apply" not in sys.argv:
        print("\nDry run only. Re-run with --apply to write tests/cleanup_test_data.py.")
        return 0

    TARGET.write_bytes(new.encode("utf-8"))
    if had_bom:
        print("Note: the UTF-8 BOM that was at the start of the file has been removed.")
    print("tests/cleanup_test_data.py patched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
