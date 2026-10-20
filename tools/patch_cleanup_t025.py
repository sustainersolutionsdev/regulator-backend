"""
One-off patch of tests/cleanup_test_data.py for T025. Run once from
regulator-backend:

    python tools\\patch_cleanup_t025.py            (shows what will change)
    python tools\\patch_cleanup_t025.py --apply    (writes the file)

Two additions, each matched exactly; stops without writing if any anchor is
missing or appears more than once:
  1. registers the "T025 Alert Regulation " title prefix (tests/test_alerts.py);
  2. also deletes Alerts Log entries (tenants/{id}/alertsLog) written for test
     regulations - matched by the same title prefixes - so every test run's
     alert entries are removed along with the regulations themselves.

Line endings are preserved. Safe to re-run.
"""
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "tests" / "cleanup_test_data.py"
MARKER = "# T025: Alerts Log entries written for test regulations."

PREFIX_ANCHOR = '    "T015 Perf Regulation ",     # perf_seed_roadmap.py (NFR-4 load data)'
PREFIX_ADD = '    "T025 Alert Regulation ",    # test_alerts.py'
USERS_ANCHOR = ["            if _is_test_user(doc.to_dict())", "        ]"]
APPLY_ANCHOR = "            _delete_refs(bu_refs)"
TOTALS_ANCHOR = '        totals["business_units"] += len(bu_refs)'


def main() -> int:
    raw = TARGET.read_bytes()
    text = raw.decode("utf-8-sig")
    nl = "\r\n" if "\r\n" in text else "\n"

    if MARKER in text:
        print("cleanup_test_data.py is already patched for T025. Nothing to do.")
        return 0

    users_anchor = nl.join(USERS_ANCHOR) + nl
    anchors = {
        "title prefix list": PREFIX_ANCHOR + nl,
        "test-user list": users_anchor,
        "apply block": APPLY_ANCHOR + nl,
        "totals": TOTALS_ANCHOR + nl,
    }
    problems = [f"{k}: expected 1 match, found {text.count(v)}" for k, v in anchors.items() if text.count(v) != 1]
    if problems:
        print("NOT PATCHED - cleanup_test_data.py does not look as expected:")
        for p in problems:
            print("  -", p)
        return 1

    alert_block = nl.join([
        "",
        "        " + MARKER,
        "        alert_refs = [",
        "            doc.reference",
        '            for doc in tenant_ref.collection("alertsLog").stream()',
        '            if str((doc.to_dict() or {}).get("regulationTitle", "")).startswith(REGULATION_TITLE_PREFIXES)',
        "        ]",
        "",
    ])
    new = text.replace(PREFIX_ANCHOR + nl, PREFIX_ANCHOR + nl + PREFIX_ADD + nl, 1)
    new = new.replace(users_anchor, users_anchor + alert_block, 1)
    new = new.replace(APPLY_ANCHOR + nl, APPLY_ANCHOR + nl + "            _delete_refs(alert_refs)" + nl, 1)
    new = new.replace(
        TOTALS_ANCHOR + nl,
        TOTALS_ANCHOR + nl + '        totals["alert_logs"] = totals.get("alert_logs", 0) + len(alert_refs)' + nl,
        1,
    )

    print('1. Register the "T025 Alert Regulation " prefix')
    print("2. Delete Alerts Log entries for test regulations")
    if "--apply" not in sys.argv:
        print("\nDry run only. Re-run with --apply to write the file.")
        return 0
    TARGET.write_bytes(new.encode("utf-8"))
    print("tests/cleanup_test_data.py patched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
