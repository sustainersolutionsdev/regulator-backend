from dotenv import load_dotenv
load_dotenv()
from google.cloud import firestore
from datetime import datetime, timezone
from core.audit import write_with_audit

db = firestore.Client(project="regulator-dev")


def provision_tenant(tenant_id: str, tenant_name: str) -> None:
    """
    The ONLY sanctioned path for creating a new tenant.
    Seeds the tenant root doc and a default Business Unit.
    No other code path should write directly to tenants/{tenantId}.
    """
    tenant_ref = db.collection("tenants").document(tenant_id)
    if tenant_ref.get().exists:
        raise ValueError(f"Tenant '{tenant_id}' already exists.")

    tenant_ref.set({
        "name": tenant_name,
        "createdAt": datetime.now(timezone.utc),
    })

    create_business_unit(tenant_id, "BU1", "Default")
    print(f"Tenant '{tenant_id}' provisioned with default BU1.")


def create_business_unit(tenant_id: str, bu_code: str, label: str) -> None:
    """
    Adds a Business Unit under an existing tenant.
    BU codes are tenant-scoped only — never cross tenants (FR-0.2).
    """
    tenant_ref = db.collection("tenants").document(tenant_id)
    if not tenant_ref.get().exists:
        raise ValueError(f"Tenant '{tenant_id}' does not exist.")

    bu_ref = tenant_ref.collection("businessUnits").document(bu_code)
    if bu_ref.get().exists:
        raise ValueError(f"Business Unit '{bu_code}' already exists for tenant '{tenant_id}'.")

    bu_ref.set({
        "code": bu_code,
        "label": label,
        "createdAt": datetime.now(timezone.utc),
    })
    print(f"Business Unit '{bu_code}' created for tenant '{tenant_id}'.")
    
def list_business_units(tenant_id: str) -> list[dict]:
    """
    Lists all Business Units under a tenant. Tenant-scoped only —
    caller must already have a verified tenant_id from RequestContext;
    this function takes no path a client could redirect cross-tenant.
    """
    tenant_ref = db.collection("tenants").document(tenant_id)
    if not tenant_ref.get().exists:
        raise ValueError(f"Tenant '{tenant_id}' does not exist.")

    return [
        {"id": doc.id, **doc.to_dict()}
        for doc in tenant_ref.collection("businessUnits").stream()
    ]    


def list_tenants() -> list[dict]:
    """
    Stub for later admin tooling (not exposed anywhere in beta UI —
    just a dev/debug helper for now).
    """
    return [
        {"id": doc.id, **doc.to_dict()}
        for doc in db.collection("tenants").stream()
    ]


# Corrected against Srinivas's actual "Input form to Add Regulation" doc —
# superseding the frozen Requirements Document's FR-0.7, which had drifted
# from this source. abbreviated_title, country, region, notes, and
# documents are treated as optional (see below); everything else here is
# required. That required/optional split is inferred, not explicitly
# marked in the source doc — flagged as such, not presented as settled.
REGULATION_REQUIRED_FIELDS = [
    "title",
    "due_date",
    "domain",
    "business_unit_ids",
]


def validate_regulation_payload(payload: dict) -> list[str]:
    """
    Returns a list of missing/empty-required-field error messages
    (empty list = valid). Kept separate from create_regulation() so
    main.py can return one 400 with every problem listed, not just
    the first field it happens to hit.
    """
    errors = []
    for field in REGULATION_REQUIRED_FIELDS:
        if field not in payload or payload[field] in (None, "", []):
            errors.append(f"'{field}' is required")
    return errors


def create_regulation(
    tenant_id: str,
    payload: dict,
    actor_uid: str,
    actor_role: str,
) -> dict:
    """
    Creates a regulation record under tenants/{tenant_id}/regulations/{reg_id},
    per Srinivas's iWant doc field set (title, abbreviated title, due date,
    domain, country, region, business units impacted, notes, uploaded
    documents, add-to-Roadmap/Dashboard toggle).

    id, createdAt, and lastUpdated are ALWAYS server-generated here —
    FR-0.8 requires these are never hand-entered by the SME (this part
    is unaffected by the FR-0.7 field-list correction), so no
    caller-supplied value for any of the three is ever read from
    `payload`, even if present. createdAt also satisfies the iWant doc's
    own "record the date of entry" note — same field, no separate work.

    Routes through write_with_audit() exactly like hide_user() and
    create_business_unit()'s sibling calls, so the record and its
    D6 audit log entry land in one transaction.
    """
    tenant_ref = db.collection("tenants").document(tenant_id)
    if not tenant_ref.get().exists:
        raise ValueError(f"Tenant '{tenant_id}' does not exist.")

    now = datetime.now(timezone.utc)

    # Firestore auto-generates the doc ID (same pattern as audit.py's
    # audit_ref) — this is the one and only source of the regulation's id.
    reg_ref = tenant_ref.collection("regulations").document()

    record = {
        "title": payload["title"],
        "abbreviatedTitle": payload.get("abbreviated_title", ""),
        "dueDate": payload["due_date"],
        "domain": payload["domain"],
        "country": payload.get("country", ""),
        "region": payload.get("region", ""),
        "businessUnitIds": payload["business_unit_ids"],
        "notes": payload.get("notes", ""),
        # Remapped to camelCase for storage — matches every other field's
        # convention. Incoming from the API as snake_case (the endpoint's
        # own contract), but Firestore should stay consistently camelCase
        # throughout, same as businessUnitIds/createdAt/etc.
        "documents": [
            {
                "fileName": d["file_name"],
                "gcsUri": d["gcs_uri"],
                "contentType": d["content_type"],
                "sizeBytes": d["size_bytes"],
                "uploadedBy": d["uploaded_by"],
                "uploadedAt": d["uploaded_at"],
            }
            for d in payload.get("documents", [])
        ],
        "addToRoadmapDashboard": payload.get("add_to_roadmap_dashboard", False),
        "createdAt": now,
        "lastUpdated": now,
        "createdBy": actor_uid,
        "hidden": False,  # NFR-8 soft-delete convention, same as users
    }

    write_with_audit(
        tenant_id=tenant_id,
        doc_ref=reg_ref,
        data=record,
        actor_uid=actor_uid,
        actor_role=actor_role,
        action="create_regulation",
    )

    print(f"Regulation '{record['title']}' created (id={reg_ref.id}, tenant={tenant_id}).")
    return {"id": reg_ref.id, **record}


if __name__ == "__main__":
    provision_tenant("test-tenant-001", "Test Tenant Inc.")
    create_business_unit("test-tenant-001", "BU2", "Printers")
    print(list_tenants())