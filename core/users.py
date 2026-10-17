from dotenv import load_dotenv
load_dotenv()
import logging
import time
import firebase_admin
import requests
from fastapi import HTTPException
from firebase_admin import credentials, auth, exceptions as firebase_exceptions
from google.api_core import exceptions as google_exceptions
from google.cloud import firestore
from datetime import datetime, timezone
from typing import Optional
import os
import secrets
from core.audit import write_with_audit

logger = logging.getLogger(__name__)


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
if not firebase_admin._apps:
    cred = credentials.Certificate(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    firebase_admin.initialize_app(cred)

db = firestore.Client(project="regulator-dev")

VALID_ROLES = {"admin", "sme", "user"}

# --- T022: dropped connections to Firebase / Firestore ------------------------
#
# Adding a user takes several calls to Google (create the login, set its role
# and Business Units, make the password link, save the directory record). On
# Oct 16 2026 a dropped connection in the middle returned a bare 500 and left
# a login with no role. Now each call is retried once, and if the user still
# can't be fully added, the half-made login is removed so nothing is left
# behind, and the caller gets a clear "please try again" (503).

# Network-level failures worth one retry. Everything else (e.g. "email
# already exists", a bad argument) is a real answer and is never retried.
TRANSIENT_ERRORS = (
    firebase_exceptions.UnavailableError,
    firebase_exceptions.DeadlineExceededError,
    firebase_exceptions.InternalError,
    google_exceptions.ServiceUnavailable,
    google_exceptions.DeadlineExceeded,
    google_exceptions.InternalServerError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    ConnectionError,
    TimeoutError,
)
RETRY_DELAY_SECONDS = 1.0

# A login found under this email counts as "ours, from the attempt that just
# dropped" only if it has no role yet and was created this recently.
ADOPT_WINDOW_SECONDS = 300

TRY_AGAIN_MESSAGE = (
    "The user couldn't be added because the connection to the sign-in service "
    "dropped. Nothing was saved. Please try again."
)


class UserProvisioningError(RuntimeError):
    """The user could not be fully added; anything partly made was removed."""


def _call(step: str, fn, *args, **kwargs):
    """Runs one call to Google, retrying once on a dropped connection."""
    try:
        return fn(*args, **kwargs)
    except TRANSIENT_ERRORS as first:
        logger.warning("Add user: %s failed (%s); retrying once.", step, type(first).__name__)
        time.sleep(RETRY_DELAY_SECONDS)
        return fn(*args, **kwargs)


def _adoptable_login(email: str):
    """
    After a dropped create, the login may exist even though no answer came
    back. Returns it only if it is clearly that half-made login: no role
    claims yet and created within the last few minutes. Otherwise None.
    """
    try:
        user = auth.get_user_by_email(email)
    except auth.UserNotFoundError:
        return None
    created_ms = (user.user_metadata.creation_timestamp or 0) if user.user_metadata else 0
    fresh = (time.time() * 1000 - created_ms) <= ADOPT_WINDOW_SECONDS * 1000
    return user if fresh and not user.custom_claims else None


def _remove_half_made_login(uid: str) -> None:
    try:
        _call("remove half-made login", auth.delete_user, uid)
    except auth.UserNotFoundError:
        pass
    except Exception:  # noqa: BLE001 - leave it for cleanup, but say so
        logger.error("Add user: could not remove half-made login %s; remove it by hand.", uid)


def _create_login(email: str, password: str, display_name: str) -> str:
    """
    Step 1, with its own retry: if the first create's answer was lost, the
    login may already exist, so the retry can hit "email already exists" for
    the login it made itself. That login is picked up only if it is clearly
    the half-made one (no role, just created); otherwise the duplicate is
    real and the usual 409 follows. A duplicate on the FIRST attempt is
    always real.
    """
    try:
        return auth.create_user(email=email, password=password, display_name=display_name).uid
    except TRANSIENT_ERRORS as first:
        logger.warning("Add user: create login failed (%s); retrying once.", type(first).__name__)
        time.sleep(RETRY_DELAY_SECONDS)
    try:
        return auth.create_user(email=email, password=password, display_name=display_name).uid
    except auth.EmailAlreadyExistsError:
        adopted = _adoptable_login(email)
        if adopted is None:
            raise
        logger.warning("Add user: picked up the login made by the dropped attempt (%s).", adopted.uid)
        return adopted.uid


def _provision_user(
    email: str,
    password: str,
    tenant_id: str,
    role: str,
    business_unit_ids: list[str],
    display_name: str,
    title: str,
    notes: str,
    with_reset_link: bool,
) -> tuple[str, Optional[str]]:
    if role not in VALID_ROLES:
        raise ValueError(f"Invalid role '{role}'. Must be one of {VALID_ROLES}.")

    tenant_ref = db.collection("tenants").document(tenant_id)
    if not _call("check tenant", tenant_ref.get).exists:
        raise ValueError(f"Tenant '{tenant_id}' does not exist.")

    # Admin/SME are tenant-wide by definition (FR-0.4) - force full BU scope
    # regardless of what was passed in, so edit-scope can never drift from role.
    if role in ("admin", "sme"):
        bu_docs = _call("list Business Units", lambda: list(tenant_ref.collection("businessUnits").stream()))
        business_unit_ids = [doc.id for doc in bu_docs]

    # 1. Create the Firebase Auth login.
    try:
        uid = _create_login(email, password, display_name or email)
    except TRANSIENT_ERRORS as e:
        raise UserProvisioningError(TRY_AGAIN_MESSAGE) from e

    # 2-4. Everything after the login exists: on failure, remove the login so
    # no half-made account is left behind.
    try:
        # 2. Custom claims - what the FastAPI middleware reads.
        _call("set role", auth.set_custom_user_claims, uid, {
            "tenant_id": tenant_id,
            "role": role,
            "businessUnitIds": business_unit_ids,
        })

        # 3. Password link (before the directory record, so a failure here
        # never leaves a record behind).
        reset_link = _call("make password link", auth.generate_password_reset_link, email) if with_reset_link else None

        # 4. Directory record (FR-0.9). Written last.
        _call("save directory record", tenant_ref.collection("users").document(uid).set, {
            "email": email,
            "displayName": display_name or email,
            "role": role,
            "businessUnitIds": business_unit_ids,
            "title": title,
            "notes": notes,
            "createdAt": datetime.now(timezone.utc),
        })
    except Exception as e:
        _remove_half_made_login(uid)
        if isinstance(e, TRANSIENT_ERRORS):
            raise UserProvisioningError(TRY_AGAIN_MESSAGE) from e
        raise

    print(f"User '{email}' created (uid={uid}, role={role}, tenant={tenant_id}).")
    return uid, reset_link


def create_user(
    email: str,
    password: str,
    tenant_id: str,
    role: str,
    business_unit_ids: list[str],
    display_name: str = "",
    title: str = "",
    notes: str = "",
) -> str:
    """
    The ONLY sanctioned path for creating a user.
    Creates the Firebase Auth account, sets custom claims
    (tenant_id, role, businessUnitIds), and mirrors the record
    into Firestore under tenants/{tenantId}/users/{userId}.

    title/notes (per Srinivas's Add Users doc, confirmed 9/16) are
    directory metadata only - not security-relevant like role/BU, so
    they go in the Firestore mirror doc only, never in custom claims.

    T022: each step retries once on a dropped connection; if the user still
    can't be fully added, the half-made login is removed and
    UserProvisioningError is raised.
    """
    uid, _ = _provision_user(
        email, password, tenant_id, role, business_unit_ids,
        display_name, title, notes, with_reset_link=False,
    )
    return uid


def update_user_role(uid: str, tenant_id: str, new_role: str, business_unit_ids: list[str] = None) -> None:
    """
    Updates a user's role/BU scope. Custom claims won't take effect
    client-side until the user's ID token refreshes (next login or
    forced refresh) - this is a known Firebase Auth behavior, not a bug.
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

def _generate_temp_password() -> str:
    """
    Generates a Firebase-Auth-valid random password (well above the
    6-char minimum). Never returned to any caller or logged - it only
    exists to satisfy Auth's required password field before being
    immediately superseded by a one-time reset link.
    """
    return secrets.token_urlsafe(18)


def create_user_and_get_reset_link(
    email: str,
    tenant_id: str,
    role: str,
    business_unit_ids: list[str],
    display_name: str = "",
    title: str = "",
    notes: str = "",
) -> tuple[str, str]:
    """
    FR-0.4 entry point for the Add Users form. Accepts no password from
    the caller and exposes no password in the response - a random temp
    password satisfies Firebase Auth internally, then a password-reset
    link is generated and handed back for the Admin/SME to pass to the
    new user out-of-band. create_user() itself is untouched, so direct/
    scripted user creation (seeding, tests) still works with an explicit
    password exactly as before.

    T022: a dropped connection that survives one retry returns 503 with
    a "please try again" message instead of a bare 500, and leaves no
    half-made login behind.
    """
    try:
        return _provision_user(
            email, _generate_temp_password(), tenant_id, role, business_unit_ids,
            display_name, title, notes, with_reset_link=True,
        )
    except (UserProvisioningError,) + TRANSIENT_ERRORS:
        # Also covers a dropped connection before the login was made
        # (tenant / Business Unit lookup) - nothing was saved either way.
        raise HTTPException(status_code=503, detail=TRY_AGAIN_MESSAGE)

if __name__ == "__main__":
    uid = create_user(
        email="admin@testtenant.com",
        password="TempPassword123!",
        tenant_id="test-tenant-001",
        role="admin",
        business_unit_ids=[],  # ignored for admin - auto-set to all BUs
        display_name="Test Admin",
    )
    print(f"Created uid: {uid}")
