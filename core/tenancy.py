from dotenv import load_dotenv
load_dotenv()
from google.cloud import firestore
from datetime import datetime, timezone

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


def list_tenants() -> list[dict]:
    """
    Stub for later admin tooling (not exposed anywhere in beta UI —
    just a dev/debug helper for now).
    """
    return [
        {"id": doc.id, **doc.to_dict()}
        for doc in db.collection("tenants").stream()
    ]


if __name__ == "__main__":
    provision_tenant("test-tenant-001", "Test Tenant Inc.")
    create_business_unit("test-tenant-001", "BU2", "Printers")
    print(list_tenants())