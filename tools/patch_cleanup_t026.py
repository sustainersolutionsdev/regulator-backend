"""
One-off patch of tests/cleanup_test_data.py for T026 (needs the T025 patch
applied first). Run once from regulator-backend:

    python tools\\patch_cleanup_t026.py            (shows what will change)
    python tools\\patch_cleanup_t026.py --apply    (writes the file)

T025 deleted Alerts Log entries for test regulations by title prefix. An
"updated" alert records the regulation's title AFTER the edit, and some
tests rename their regulation, so those entries could be left behind. This
also deletes any Alerts Log entry whose regulation is one of the test
regulations being deleted in the same run.

Line endings are preserved. Safe to re-run.
"""
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "tests" / "cleanup_test_data.py"
MARKER = "# T026: also match by regulation id"

OLD = '            if str((doc.to_dict() or {}).get("regulationTitle", "")).startswith(REGULATION_TITLE_PREFIXES)'
NEW = [
    "            " + MARKER + " (an update alert records the edited title).",
    '            if (doc.to_dict() or {}).get("regulationId") in test_reg_ids',
    '            or str((doc.to_dict() or {}).get("regulationTitle", "")).startswith(REGULATION_TITLE_PREFIXES)',
]
OLD_BLOCK_START = "        alert_refs = ["


def main() -> int:
    text = TARGET.read_bytes().decode("utf-8-sig")
    nl = "\r\n" if "\r\n" in text else "\n"
    if MARKER in text:
        print("cleanup_test_data.py is already patched for T026. Nothing to do.")
        return 0
    if "# T025: Alerts Log entries written for test regulations." not in text:
        print("NOT PATCHED - apply tools\\patch_cleanup_t025.py first.")
        return 1
    problems = [
        f"{name}: expected 1 match, found {text.count(a)}"
        for name, a in (("title condition", OLD + nl), ("alert_refs block", OLD_BLOCK_START + nl))
        if text.count(a) != 1
    ]
    if problems:
        print("NOT PATCHED - cleanup_test_data.py does not look as expected:")
        for p in problems:
            print("  -", p)
        return 1

    new = text.replace(
        OLD_BLOCK_START + nl,
        "        test_reg_ids = {ref.id for ref in reg_refs}" + nl + OLD_BLOCK_START + nl,
        1,
    )
    new = new.replace(OLD + nl, nl.join(NEW) + nl, 1)
    print("1. Delete Alerts Log entries of test regulations by regulation id as well as by title")
    if "--apply" not in sys.argv:
        print("\nDry run only. Re-run with --apply to write the file.")
        return 0
    TARGET.write_bytes(new.encode("utf-8"))
    print("tests/cleanup_test_data.py patched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
