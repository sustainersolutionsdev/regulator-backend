"""
One-off repair for T025 (found Oct 20 2026). Run from regulator-backend:

    python tools\\fix_t025_restore_test_user.py            (shows the record)
    python tools\\fix_t025_restore_test_user.py --apply    (restores it)

The fixed User test account user@testtenant.com can still sign in, but its
directory record (tenants/test-tenant-001/users/<uid>) is marked hidden -
i.e. soft-removed. Email alerts rightly skip removed users, so
tests/test_alerts.py::test_user_with_alerts_off_is_skipped failed: the user
was dropped as removed before its alerts-off setting was read.

This restores exactly that one record (hidden -> False), through
write_with_audit() so the change is audited (NFR-2). The previous
hiddenAt/hiddenBy values stay in the audit entry's "before". Nothing else is
touched. Refuses to run outside regulator-dev / test-tenant-001.

Side effect: user@testtenant.com reappears in the People Directory of
test-tenant-001, as an active test user should.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from datetime import datetime, timezone

import firebase_admin
from firebase_admin import auth, credentials

if not firebase_admin._apps:
    firebase_admin.initialize_app(credentials.Certificate(os.environ["GOOGLE_APPLICATION_CREDENTIALS"]))

from core.audit import write_with_audit
from core.deps import db

PROJECT = "regulator-dev"
TENANT = "test-tenant-001"
EMAIL = "user@testtenant.com"


def main() -> int:
    if db.project != PROJECT:
        print(f"Refusing to run against project '{db.project}'.")
        return 1

    login = auth.get_user_by_email(EMAIL)
    if (login.custom_claims or {}).get("tenant_id") != TENANT:
        print(f"Refusing: {EMAIL} is not in {TENANT}.")
        return 1

    ref = db.collection("tenants").document(TENANT).collection("users").document(login.uid)
    snap = ref.get()
    if not snap.exists:
        print(f"No directory record users/{login.uid}; nothing to restore.")
        return 1
    data = snap.to_dict()
    print(f"users/{login.uid}  email={data.get('email')!r}  hidden={data.get('hidden')}  "
          f"hiddenAt={data.get('hiddenAt')}  hiddenBy={data.get('hiddenBy')}")
    if not data.get("hidden"):
        print("Already active. Nothing to do.")
        return 0

    if "--apply" not in sys.argv:
        print("\nDry run only. Re-run with --apply to restore it.")
        return 0

    write_with_audit(
        tenant_id=TENANT,
        doc_ref=ref,
        data={"hidden": False, "restoredAt": datetime.now(timezone.utc), "restoredBy": "maintenance:T025"},
        actor_uid="maintenance:T025",
        actor_role="system",
        action="restore_test_user",
    )
    print(f"Restored: {EMAIL} is an active directory record again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
