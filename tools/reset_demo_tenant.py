"""
tools/reset_demo_tenant.py - T078: wipe a demo/testing company workspace on
regulator-dev completely, so it can be rebuilt clean for business testing
(decided Nov 15, 2026: "delete everything we created for Srinivas and
Niranjan for the testing, and build it again"; scope: sustainer-demo only,
full rebuild - Mahesh, Nov 15).

What it deletes, for ONE tenant:
  - every Firestore document under tenants/<id> (regulations and their
    activity, Business Units, users, preferences, audit log, alerts log,
    AI usage, saved AI summaries - every subcollection, found by walking
    the tree, not from a fixed list) and the tenant document itself;
  - every uploaded file under tenants/<id>/ in the documents bucket;
  - every login (Firebase Auth) whose role claims name this tenant, plus
    any login listed in its directory.

Hard deletes are deliberate: NFR-8's soft-delete rule protects customer
data; this is disposable test data (same rule as tests/cleanup_test_data.py).

Safety:
  - dry run by default: prints exactly what would go, deletes nothing;
  - refuses any project other than regulator-dev;
  - refuses test-tenant-001 and any tenant holding the automated tests'
    accounts (@testtenant.com, @tenantb.com);
  - --apply also needs --confirm <tenant id>, typed again;
  - before deleting, writes a full backup to --backup-dir (default
    %USERPROFILE%\\regulator-backups): every document as JSON, the logins'
    uid/email/claims (never passwords), and a copy of every uploaded file.
    Keep the backup out of git.

Usage (PowerShell, backend venv active, from the regulator-backend folder):
  python tools\\reset_demo_tenant.py --tenant-id sustainer-demo
  python tools\\reset_demo_tenant.py --tenant-id sustainer-demo --apply --confirm sustainer-demo

Rebuild afterwards with tools\\provision_demo_tenant.py (see its usage).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

ALLOWED_PROJECT = "regulator-dev"
PROTECTED_TENANTS = {"test-tenant-001"}
TEST_ACCOUNT_DOMAINS = ("@testtenant.com", "@tenantb.com")


class Refused(Exception):
    pass


def check_allowed(project: str, tenant_id: str, apply: bool, confirm: str | None, emails: list[str]) -> None:
    """Every refusal in one place, so it can be tested without Google."""
    if project != ALLOWED_PROJECT:
        raise Refused(f"this tool is for {ALLOWED_PROJECT}, but GCP_PROJECT is '{project}'.")
    if not tenant_id or tenant_id in PROTECTED_TENANTS:
        raise Refused(f"'{tenant_id}' is protected (the automated tests use it).")
    test_accounts = [e for e in emails if e.lower().endswith(TEST_ACCOUNT_DOMAINS)]
    if test_accounts:
        raise Refused(f"'{tenant_id}' holds automated-test accounts ({', '.join(sorted(test_accounts))}).")
    if apply and confirm != tenant_id:
        raise Refused(f"--apply needs --confirm {tenant_id} (the tenant id typed again).")


def walk(doc_ref, out: list | None = None) -> list:
    """Every document at and under doc_ref (depth first), as (ref, snapshot-or-None)."""
    out = [] if out is None else out
    for coll in doc_ref.collections():
        for snap in coll.stream():
            out.append((snap.reference, snap))
            walk(snap.reference, out)
    return out


def _json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "path"):  # DocumentReference
        return value.path
    return str(value)


def inventory(db, auth, bucket, tenant_id: str) -> dict:
    tenant_ref = db.collection("tenants").document(tenant_id)
    tenant_snap = tenant_ref.get()
    docs = walk(tenant_ref)
    directory_uids = {
        ref.id for ref, _ in docs if ref.parent.id == "users" and ref.parent.parent.id == tenant_id
    }
    logins = []
    for user in auth.list_users().iterate_all():
        claims = user.custom_claims or {}
        if claims.get("tenant_id") == tenant_id or user.uid in directory_uids:
            logins.append(user)
    blobs = list(bucket.list_blobs(prefix=f"tenants/{tenant_id}/"))
    return {"tenant_ref": tenant_ref, "tenant_snap": tenant_snap, "docs": docs, "logins": logins, "blobs": blobs}


def summary(inv: dict) -> dict:
    counts: dict[str, int] = {}
    for ref, _ in inv["docs"]:
        counts[ref.parent.id] = counts.get(ref.parent.id, 0) + 1
    return {
        "tenant_document": bool(inv["tenant_snap"].exists),
        "documents_by_collection": dict(sorted(counts.items())),
        "documents_total": len(inv["docs"]) + (1 if inv["tenant_snap"].exists else 0),
        "logins": sorted((u.email or u.uid) for u in inv["logins"]),
        "uploaded_files": len(inv["blobs"]),
    }


def backup(inv: dict, tenant_id: str, backup_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"{tenant_id}-{stamp}"
    (target / "files").mkdir(parents=True, exist_ok=False)
    with open(target / "firestore.jsonl", "w", encoding="utf-8") as fh:
        if inv["tenant_snap"].exists:
            fh.write(json.dumps({"path": inv["tenant_ref"].path, "data": inv["tenant_snap"].to_dict()},
                                default=_json_default) + "\n")
        for ref, snap in inv["docs"]:
            fh.write(json.dumps({"path": ref.path, "data": snap.to_dict()}, default=_json_default) + "\n")
    with open(target / "logins.json", "w", encoding="utf-8") as fh:
        json.dump([{"uid": u.uid, "email": u.email, "displayName": u.display_name, "claims": u.custom_claims}
                   for u in inv["logins"]], fh, indent=1, default=_json_default)
    for blob in inv["blobs"]:
        dest = target / "files" / blob.name.replace("/", "__")
        blob.download_to_filename(str(dest))
    return target


def delete_all(db, auth, inv: dict) -> None:
    for user in inv["logins"]:
        try:
            auth.delete_user(user.uid)
        except auth.UserNotFoundError:
            pass
    for blob in inv["blobs"]:
        blob.delete()
    refs = [ref for ref, _ in inv["docs"]]
    refs.sort(key=lambda r: -r.path.count("/"))  # children before parents
    batch, pending = db.batch(), 0
    for ref in refs + [inv["tenant_ref"]]:
        batch.delete(ref)
        pending += 1
        if pending == 450:  # Firestore batch limit is 500
            batch.commit()
            batch, pending = db.batch(), 0
    if pending:
        batch.commit()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant-id", required=True)
    p.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    p.add_argument("--confirm", help="the tenant id typed again; required with --apply")
    p.add_argument("--backup-dir", default=str(Path.home() / "regulator-backups"))
    args = p.parse_args(argv)

    from firebase_admin import auth
    from google.cloud import storage

    from core.config import DOCUMENTS_BUCKET, GCP_PROJECT, init_firebase

    init_firebase()
    from core.clients import db

    bucket = storage.Client(project=GCP_PROJECT).bucket(DOCUMENTS_BUCKET)
    inv = inventory(db, auth, bucket, args.tenant_id)
    emails = [u.email or "" for u in inv["logins"]]
    try:
        check_allowed(GCP_PROJECT, args.tenant_id, args.apply, args.confirm, emails)
    except Refused as e:
        print(f"Refusing: {e}")
        return 2

    info = summary(inv)
    mode = "APPLY" if args.apply else "DRY RUN (nothing deleted; add --apply --confirm <tenant id>)"
    print(f"[reset] {mode} - project {GCP_PROJECT}, tenant '{args.tenant_id}'")
    print(json.dumps(info, indent=2))
    if not info["tenant_document"] and not info["documents_total"] and not info["logins"] and not info["uploaded_files"]:
        print("[reset] nothing to delete.")
        return 0
    if not args.apply:
        return 0

    target = backup(inv, args.tenant_id, Path(args.backup_dir))
    print(f"[reset] backup written to {target}  (keep it out of git)")
    delete_all(db, auth, inv)
    after = summary(inventory(db, auth, bucket, args.tenant_id))
    print("[reset] after:", json.dumps(after))
    left = after["documents_total"] or after["logins"] or after["uploaded_files"]
    print("[reset] done - workspace removed." if not left else "[reset] WARNING: something is left; run again.")
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())
