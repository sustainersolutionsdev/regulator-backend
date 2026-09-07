from google.cloud import firestore
from datetime import datetime, timezone

db = firestore.Client(project="regulator-dev")


@firestore.transactional
def _write_with_audit_txn(transaction, doc_ref, audit_ref, data, audit_entry):
    """
    Writes the primary document and its audit entry in the SAME
    transaction (NFR-2) — a partial failure can never produce a
    record change with no audit trail, or an audit trail with no
    corresponding change.
    """
    transaction.set(doc_ref, data, merge=True)
    transaction.set(audit_ref, audit_entry)


def write_with_audit(
    tenant_id: str,
    doc_ref: firestore.DocumentReference,
    data: dict,
    actor_uid: str,
    actor_role: str,
    action: str,
) -> None:
    """
    The ONLY sanctioned way to write a regulation/status/note/document
    change. Every call produces exactly one D6 Audit Log entry,
    atomically with the write it describes.
    """
    before_snapshot = doc_ref.get()
    before = before_snapshot.to_dict() if before_snapshot.exists else None

    audit_ref = db.collection("tenants").document(tenant_id) \
        .collection("auditLog").document()

    audit_entry = {
        "actorUid": actor_uid,
        "actorRole": actor_role,
        "action": action,
        "targetPath": doc_ref.path,
        "before": before,
        "after": data,
        "timestamp": datetime.now(timezone.utc),
    }

    transaction = db.transaction()
    _write_with_audit_txn(transaction, doc_ref, audit_ref, data, audit_entry)
    print(f"[audit] {action} on {doc_ref.path} by {actor_uid} ({actor_role})")