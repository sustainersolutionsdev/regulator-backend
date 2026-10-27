"""
core/invitations.py - the welcome / invitation email for new users (T061,
OPEN-16 part 1; approved by Srinivas Oct 26, option A, wording as proposed).

Found Oct 25: Add Users created the account but told the person nothing,
and the API handed the one-time password link back to the Admin's browser.
Now:
  - POST /users schedules an invitation; the link never leaves the server.
  - POST /users/{uid}/resend-invitation (Admin/SME) sends a fresh one.
  - Deployed, the work goes through Pub/Sub to the Alerts Worker (event
    "user.invited", which carries IDs only - no email address, no link). The
    worker makes the password link itself, so it is never stored or queued.
  - It is an account email, not an alert: sent whatever the person's email
    alert setting is. It follows ALERTS_MODE (send / log / off) and never
    emails the test domains, like alerts.
  - Every invitation writes an Alerts Log entry (user_invited /
    invitation_resent) with user IDs only - never the address or the link.

The password link is Firebase's; it works for 1 hour (Firebase's fixed
limit), so the email also explains "Forgot password?" for later.
"""
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from core import alerts

logger = logging.getLogger(__name__)

ROLE_LABELS = {"admin": "Admin", "sme": "SME", "user": "User"}


def _scope_text(person: dict, bu_labels: dict) -> str:
    """Admin/SME cover every Business Unit (FR-0.4); a User gets their own list."""
    if person.get("role") in ("admin", "sme"):
        return "all Business Units"
    names = [bu_labels.get(code) or code for code in (person.get("businessUnitIds") or [])]
    if not names:
        return "no Business Units yet"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def render_invitation(person: dict, *, tenant_name: str, bu_labels: dict, actor: dict,
                      link: str, app_base_url: str) -> dict:
    """The approved wording (Oct 26). Returns {subject, html, text}; every value is escaped."""
    e = alerts._e
    name = alerts._one_line(person.get("displayName")) or person.get("email", "")
    company = alerts._one_line(tenant_name)
    admin = alerts._one_line(actor.get("displayName")) or "An administrator"
    role = ROLE_LABELS.get(person.get("role"), "User")
    scope = _scope_text(person, bu_labels)
    subject = alerts._one_line(f"You've been added to Regulator — {company}")[:200]

    text = (
        f"Hello {name},\n\n"
        f"{admin} has added you to Regulator for {company} as {role}, for {scope}.\n"
        "Regulator tracks the regulations that apply to your company, their due dates and each "
        "Business Unit's compliance status.\n\n"
        f"Set my password: {link}\n\n"
        f"This button works for 1 hour. If it has expired, open {app_base_url}, choose \"Forgot password?\" "
        "and enter this email address.\n\n"
        "If you weren't expecting this email, you can ignore it; no one can sign in as you without setting "
        "a password from your inbox.\n"
    )
    font = alerts.FONT
    html = f"""<!doctype html><html><body style="margin:0;padding:0;background:#F1F5F9;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F1F5F9;padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#FFFFFF;border:1px solid #CBD5E1;border-radius:8px;font-family:{font};color:#0F172A;">
<tr><td style="background:#0F172A;padding:16px 24px;border-radius:8px 8px 0 0;color:#FFFFFF;font-size:16px;font-weight:700;">Regulator <span style="font-weight:400;color:#CBD5E1;font-size:12px;">by Sustainer</span></td></tr>
<tr><td style="padding:24px;font-size:15px;line-height:1.55;">
<p style="margin:0 0 12px;">Hello {e(name)},</p>
<p style="margin:0 0 12px;"><strong>{e(admin)}</strong> has added you to Regulator for <strong>{e(company)}</strong> as <strong>{e(role)}</strong>, for {e(scope)}.</p>
<p style="margin:0 0 20px;">Regulator tracks the regulations that apply to your company, their due dates and each Business Unit's compliance status.</p>
<p style="margin:0 0 20px;"><a href="{e(link)}" style="display:inline-block;background:#0D7377;color:#FFFFFF;text-decoration:none;font-weight:600;padding:12px 22px;border-radius:6px;">Set my password</a></p>
<p style="margin:0 0 12px;font-size:13px;color:#475569;">This button works for 1 hour. If it has expired, open <a href="{e(app_base_url)}" style="color:#0D7377;">{e(app_base_url)}</a>, choose &ldquo;Forgot password?&rdquo; and enter this email address.</p>
<p style="margin:0;font-size:13px;color:#475569;">If you weren't expecting this email, you can ignore it; no one can sign in as you without setting a password from your inbox.</p>
</td></tr>
</table></td></tr></table></body></html>"""
    return {"subject": subject, "html": html, "text": text}


def make_password_link(email: str, app_base_url: str) -> str:
    """Firebase's one-time password link. After setting the password, Firebase's
    page offers to continue to Regulator. Never logged or stored."""
    from firebase_admin import auth

    from core.config import init_firebase

    init_firebase()
    settings = auth.ActionCodeSettings(url=app_base_url)
    return auth.generate_password_reset_link(email, settings)


def send_invitation(tenant_id: str, uid: str, actor_uid: str, resend: bool = False,
                    db=None, post: Optional[Callable] = None,
                    make_link: Optional[Callable] = None) -> Optional[dict]:
    """
    Sends one invitation and writes its Alerts Log entry. Runs after the
    response (or on the worker). Returns the entry, or None if nothing applies.
    Never raises.
    """
    entry_type = "invitation_resent" if resend else "user_invited"
    cfg = alerts.alert_settings()
    if cfg["mode"] == "off":
        return None
    try:
        if db is None:
            from core.deps import db  # lazy: keeps this module importable without credentials
        tenant_ref = db.collection("tenants").document(tenant_id)
        snap = tenant_ref.collection("users").document(uid).get()
        person = (snap.to_dict() or {}) if snap.exists else {}
        email = str(person.get("email") or "").strip()
        if not snap.exists or person.get("hidden") or "@" not in email:
            logger.info("Invitation: user %s in %s not found, removed or without email; nothing sent.", uid, tenant_id)
            return None

        tenant_name, bu_labels, _users, _prefs, actor = alerts._tenant_context(tenant_ref, actor_uid)
        now = datetime.now(timezone.utc)
        recipient = {"uid": uid, "email": email, "name": person.get("displayName") or email}
        domain = email.rsplit("@", 1)[-1].lower()

        if cfg["mode"] == "send" and domain not in cfg["blocked_domains"]:
            link = (make_link or make_password_link)(email, cfg["app_base_url"])
        else:
            link = cfg["app_base_url"]  # log mode / test domain: nothing is sent, so no real link is made
        message = render_invitation(person, tenant_name=tenant_name, bu_labels=bu_labels, actor=actor,
                                    link=link, app_base_url=cfg["app_base_url"])
        outcome = alerts.deliver(cfg, [recipient], message, post=post)

        entry = {
            "type": entry_type,
            "userUid": uid,
            "subject": message["subject"],
            "triggeredBy": actor_uid,
            "mode": cfg["mode"],
            "status": outcome["status"],
            "sentUids": outcome["sent"],
            "failed": outcome["failed"],
            "skippedTestDomain": outcome["skipped_test_domain"],
            "providerMessageIds": outcome["message_ids"],
            "createdAt": now,
        }
        tenant_ref.collection("alertsLog").document().set(entry)
        print(f"[invite] {entry_type} {uid}: {outcome['status']} (mode={cfg['mode']})")
        return entry
    except Exception as e:  # noqa: BLE001 - an invitation must never break anything else
        logger.exception("Invitation %s for %s failed: %s", entry_type, uid, type(e).__name__)
        try:
            db.collection("tenants").document(tenant_id).collection("alertsLog").document().set({
                "type": entry_type, "userUid": uid, "triggeredBy": actor_uid, "mode": cfg["mode"],
                "status": "failed", "error": type(e).__name__, "createdAt": datetime.now(timezone.utc),
            })
        except Exception:  # noqa: BLE001
            pass
        return None


def schedule_invitation(background_tasks, tenant_id: str, uid: str, actor_uid: str, resend: bool = False) -> None:
    """Hand-off from POST /users and the resend route: a Pub/Sub "user.invited"
    event when deployed (IDs only), BackgroundTasks locally - as for alerts."""
    event = {"type": "user.invited", "tenantId": tenant_id, "uid": uid, "actorUid": actor_uid, "resend": resend}
    alerts._publish_or_fallback(background_tasks, event, send_invitation, tenant_id, uid, actor_uid, resend)
