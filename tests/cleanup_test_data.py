"""
Test-data teardown for regulator-dev (first flagged T006, fixed before T012).

Every test run writes regulations, Business Units, users and uploaded files
into the two test tenants, and nothing ever removed them. By T011 the live
Roadmap for test-tenant-001 showed ~50 "Test Reg" rows.

This module deletes ONLY records that match the naming patterns the test
suite itself uses (listed below). Real records you add by hand through the
UI - e.g. "40 CFR 261.11" - never match and are never touched.

Two ways it runs:
  1. Automatically after every `pytest tests/` session (tests/conftest.py),
     locally and in Cloud Build.
  2. By hand, for the one-time cleanup of historical data:
        python tests\\cleanup_test_data.py            (dry run: lists only)
        python tests\\cleanup_test_data.py --apply    (deletes)

Hard deletes here are deliberate and dev-only. NFR-8's "soft delete, never
hard delete" applies to customer data; this is disposable test data in test
tenants. The guards below refuse to run against anything but the
regulator-dev project and the two test tenants.

Audit-log entries for deleted test records are kept: the audit log is never
deleted, and nothing in the UI reads it.

NEW TEST FILES: name anything your test creates with a prefix registered
below, or add your prefix here. Unregistered names are left behind.
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

import re
import firebase_admin
from firebase_admin import auth, credentials
from google.cloud import storage
from core.tenancy import db
from core.storage import BUCKET_NAME

if not firebase_admin._apps:
    firebase_admin.initialize_app(credentials.Certificate(os.environ["GOOGLE_APPLICATION_CREDENTIALS"]))

ALLOWED_PROJECT = "regulator-dev"

# The fixed test accounts. Their tenants are the only ones this module
# touches, and the accounts themselves are never deleted.
TEST_TENANT_ACCOUNTS = ("admin@testtenant.com", "admin@tenantb.com")
PROTECTED_EMAILS = {
    "admin@testtenant.com",
    "sme@testtenant.com",
    "user@testtenant.com",
    "admin@tenantb.com",
}

# --- Patterns used by the test suite ----------------------------------------
REGULATION_TITLE_PREFIXES = (
    "Test Regulation ",          # test_regulations.py
    "Required-Only Test ",       # test_regulations.py
    "T009 QC Regulation ",       # test_m0_qc_hardening.py
    "T011 Roadmap Regulation ",  # test_roadmap_regulations.py
    "T014 Edit Regulation ",     # test_regulation_edits.py
    "T018 Status Regulation ",  # test_regulation_status.py
    "T020 Isolation Regulation ",  # test_isolation.py
    "T020 Document Regulation ",  # test_regulation_documents.py
    "T022 QC Regulation ",       # test_m2_qc_hardening.py
    "T015 Perf Regulation ",     # perf_seed_roadmap.py (NFR-4 load data)
    "T025 Alert Regulation ",    # test_alerts.py
)
BU_CODE_PREFIXES = ("BU-T006-TEST-",)                     # test_business_units.py
BU_CODES_EXACT = {"BU-DUP-TEST", "BU-USER-ATTEMPT"}       # test_business_units.py
# test_users.py: unique_email() -> "<prefix>-<8 hex>@testtenant.com"
TEST_USER_EMAIL = re.compile(r"^[a-z]+-[0-9a-f]{8}@testtenant\.com$")
# Upload-only tests store "<uuid4>-<filename>" with these filenames.
TEST_UPLOAD_FILENAMES = ("test.txt", "slides.pptx", "t009.txt")
# Business Units seeded for the test tenants and relied on by tests. Never deleted.
PROTECTED_BU_CODES = {"BU1", "BU2"}


def _test_tenant_ids() -> list[str]:
    ids = set()
    for email in TEST_TENANT_ACCOUNTS:
        claims = auth.get_user_by_email(email).custom_claims or {}
        if claims.get("tenant_id"):
            ids.add(claims["tenant_id"])
    if "test-tenant-001" not in ids:
        raise RuntimeError("Refusing to run: could not resolve test-tenant-001 from the test accounts.")
    return sorted(ids)


def _is_test_regulation(data: dict) -> bool:
    return str(data.get("title", "")).startswith(REGULATION_TITLE_PREFIXES)


def _is_test_bu(code: str) -> bool:
    if code in PROTECTED_BU_CODES:
        return False
    return code in BU_CODES_EXACT or code.startswith(BU_CODE_PREFIXES)


def _is_test_user(data: dict) -> bool:
    email = str(data.get("email", "")).lower()
    return email not in PROTECTED_EMAILS and bool(TEST_USER_EMAIL.match(email))


def _is_test_upload(blob_name: str) -> bool:
    filename = blob_name.rsplit("/", 1)[-1]
    # "<36-char uuid4>-<original filename>"
    return len(filename) > 37 and filename[36] == "-" and filename[37:] in TEST_UPLOAD_FILENAMES


def _delete_refs(refs) -> None:
    batch = db.batch()
    pending = 0
    for ref in refs:
        batch.delete(ref)
        pending += 1
        if pending == 450:  # Firestore batch limit is 500
            batch.commit()
            batch = db.batch()
            pending = 0
    if pending:
        batch.commit()


def _orphan_test_logins(tenant_ids: list[str]) -> list:
    """
    T022: test logins with no directory record in any test tenant - left
    by a sign-up that failed half-way (e.g. a dropped connection to
    Firebase). Same email pattern and protected accounts as _is_test_user;
    a login whose claims name a tenant outside the test tenants is never
    touched.
    """
    orphans = []
    for user in auth.list_users().iterate_all():
        email = (user.email or "").lower()
        if email in PROTECTED_EMAILS or not TEST_USER_EMAIL.match(email):
            continue
        claimed = (user.custom_claims or {}).get("tenant_id")
        if claimed and claimed not in tenant_ids:
            continue
        if any(
            db.collection("tenants").document(t).collection("users").document(user.uid).get().exists
            for t in tenant_ids
        ):
            continue
        orphans.append(user)
    return orphans

def run_cleanup(apply: bool = False, verbose: bool = True) -> dict:
    if db.project != ALLOWED_PROJECT:
        raise RuntimeError(f"Refusing to run against project '{db.project}' (only '{ALLOWED_PROJECT}').")

    bucket = storage.Client(project=ALLOWED_PROJECT).bucket(BUCKET_NAME)
    totals = {"regulations": 0, "business_units": 0, "users": 0, "files": 0, "orphan_logins": 0}

    tenant_ids = _test_tenant_ids()
    for tenant_id in tenant_ids:
        tenant_ref = db.collection("tenants").document(tenant_id)
        tenant_prefix = f"gs://{BUCKET_NAME}/tenants/{tenant_id}/"

        # Regulations, plus the files they reference.
        reg_refs, blob_names = [], set()
        for doc in tenant_ref.collection("regulations").stream():
            data = doc.to_dict()
            if not _is_test_regulation(data):
                continue
            reg_refs.append(doc.reference)
            for d in data.get("documents") or []:
                uri = d.get("gcsUri", "")
                if uri.startswith(tenant_prefix):
                    blob_names.add(uri[len(f"gs://{BUCKET_NAME}/"):])

        # Files uploaded by upload-only tests and never attached to a record.
        for blob in bucket.list_blobs(prefix=f"tenants/{tenant_id}/regulation-documents/"):
            if _is_test_upload(blob.name):
                blob_names.add(blob.name)

        bu_refs = [
            doc.reference
            for doc in tenant_ref.collection("businessUnits").stream()
            if _is_test_bu(doc.id)
        ]

        user_docs = [
            doc for doc in tenant_ref.collection("users").stream()
            if _is_test_user(doc.to_dict())
        ]

        # T025: Alerts Log entries written for test regulations.
        test_reg_ids = {ref.id for ref in reg_refs}
        alert_refs = [
            doc.reference
            for doc in tenant_ref.collection("alertsLog").stream()
            # T026: also match by regulation id (an update alert records the edited title).
            if (doc.to_dict() or {}).get("regulationId") in test_reg_ids
            or str((doc.to_dict() or {}).get("regulationTitle", "")).startswith(REGULATION_TITLE_PREFIXES)
        ]

        if verbose:
            print(
                f"[cleanup] {tenant_id}: {len(reg_refs)} regulations, {len(bu_refs)} Business Units, "
                f"{len(user_docs)} users, {len(blob_names)} files"
                + ("" if apply else "  (dry run - nothing deleted)")
            )
            if not apply:
                for doc in user_docs:
                    print(f"           user  {doc.to_dict().get('email')}")
                for ref in bu_refs:
                    print(f"           BU    {ref.id}")

        if apply:
            _delete_refs(reg_refs)
            _delete_refs(bu_refs)
            _delete_refs(alert_refs)
            for doc in user_docs:
                try:
                    auth.delete_user(doc.id)  # mirror doc id == Firebase Auth uid
                except auth.UserNotFoundError:
                    pass
            _delete_refs([doc.reference for doc in user_docs])
            for name in blob_names:
                blob = bucket.blob(name)
                if blob.exists():
                    blob.delete()

        totals["regulations"] += len(reg_refs)
        totals["business_units"] += len(bu_refs)
        totals["alert_logs"] = totals.get("alert_logs", 0) + len(alert_refs)
        totals["users"] += len(user_docs)
        totals["files"] += len(blob_names)

    # T022: logins left with no directory record (see _orphan_test_logins).
    orphans = _orphan_test_logins(tenant_ids)
    if verbose:
        print(
            f"[cleanup] orphan test logins: {len(orphans)}"
            + ("" if apply else "  (dry run - nothing deleted)")
        )
        if not apply:
            for user in orphans:
                print(f"           login {user.email}")
    if apply:
        for user in orphans:
            try:
                auth.delete_user(user.uid)
            except auth.UserNotFoundError:
                pass
    totals["orphan_logins"] += len(orphans)
    return totals


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    result = run_cleanup(apply=apply)
    print(("[cleanup] deleted: " if apply else "[cleanup] would delete: ") + str(result))
    if not apply:
        print("[cleanup] re-run with --apply to delete.")
