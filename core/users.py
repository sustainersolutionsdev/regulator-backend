from dotenv import load_dotenv
load_dotenv()
import firebase_admin
from firebase_admin import credentials, auth, firestore as fb_firestore
from google.cloud import firestore
from datetime import datetime, timezone
import os

from core.audit import write_with_audit


def hide_user(tenant_id: str, target_uid: str, actor_uid: str, actor_role: str) -> None:
    """
    Soft-delete convention (NFR-8): sets hidden/hiddenAt/hiddenBy.
    Never a hard delete. Routes through write_with_audit so the
    action is captured in D6 exactly like any other change.
    """
    user_ref = db.collection("tenants").document(tenant_id) \
        .collection("users").document(target_uid)

    if not user_ref.get().exists:
        raise ValueError(f"User '{target_uid}' does not exist in tenant '{tenant_id}'.")

    write_with_audit(
        tenant_id=tenant_id,
        doc_ref=user_ref,
        data={
            "hidden": True,
            "hiddenAt": datetime.now(timezone.utc),
            "hiddenBy": actor_uid,
        },
        actor_uid=actor_uid,
        actor_role=actor_role,
        action="hide_user",
    )

# Initialize the Admin SDK using the same service account key
cred = credentials.Certificate(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
firebase_admin.initialize_app(cred)

db = firestore.Client(project="regulator-dev")

VALID_ROLES = {"admin", "sme", "user"}


def create_user(
    email: str,
    password: str,
    tenant_id: str,
    role: str,
    business_unit_ids: list[str],
    display_name: str = "",
) -> str:
    """
    The ONLY sanctioned path for creating a user.
    Creates the Firebase Auth account, sets custom claims
    (tenant_id, role, businessUnitIds), and mirrors the record
    into Firestore under tenants/{tenantId}/users/{userId}.
    """
    if role not in VALID_ROLES:
        raise ValueError(f"Invalid role '{role}'. Must be one of {VALID_ROLES}.")

    tenant_ref = db.collection("tenants").document(tenant_id)
    if not tenant_ref.get().exists:
        raise ValueError(f"Tenant '{tenant_id}' does not exist.")

    # Admin/SME are tenant-wide by definition (FR-0.4) — force full BU scope
    # regardless of what was passed in, so edit-scope can never drift from role.
    if role in ("admin", "sme"):
        bu_docs = tenant_ref.collection("businessUnits").stream()
        business_unit_ids = [doc.id for doc in bu_docs]

    # 1. Create the Firebase Auth account
    user_record = auth.create_user(
        email=email,
        password=password,
        display_name=display_name or email,
    )

    # 2. Set custom claims — this is what the FastAPI middleware reads
    auth.set_custom_user_claims(user_record.uid, {
        "tenant_id": tenant_id,
        "role": role,
        "businessUnitIds": business_unit_ids,
    })

    # 3. Mirror into Firestore for directory/listing purposes (FR-0.9)
    tenant_ref.collection("users").document(user_record.uid).set({
        "email": email,
        "displayName": display_name or email,
        "role": role,
        "businessUnitIds": business_unit_ids,
        "createdAt": datetime.now(timezone.utc),
    })

    print(f"User '{email}' created (uid={user_record.uid}, role={role}, tenant={tenant_id}).")
    return user_record.uid


def update_user_role(uid: str, tenant_id: str, new_role: str, business_unit_ids: list[str] = None) -> None:
    """
    Updates a user's role/BU scope. Custom claims won't take effect
    client-side until the user's ID token refreshes (next login or
    forced refresh) — this is a known Firebase Auth behavior, not a bug.
    """
    if new_role not in VALID_ROLES:
        raise ValueError(f"Invalid role '{new_role}'. Must be one of {VALID_ROLES}.")

    if new_role in ("admin", "sme"):
        tenant_ref = db.collection("tenants").document(tenant_id)
        bu_docs = tenant_ref.collection("businessUnits").stream()
        business_unit_ids = [doc.id for doc in bu_docs]

    auth.set_custom_user_claims(uid, {
        "tenant_id": tenant_id,
        "role": new_role,
        "businessUnitIds": business_unit_ids or [],
    })

    db.collection("tenants").document(tenant_id).collection("users").document(uid).update({
        "role": new_role,
        "businessUnitIds": business_unit_ids or [],
    })

    print(f"User '{uid}' updated to role={new_role}.")


if __name__ == "__main__":
    uid = create_user(
        email="admin@testtenant.com",
        password="TempPassword123!",
        tenant_id="test-tenant-001",
        role="admin",
        business_unit_ids=[],  # ignored for admin — auto-set to all BUs
        display_name="Test Admin",
    )
    print(f"Created uid: {uid}")