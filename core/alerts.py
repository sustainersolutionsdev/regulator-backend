"""
T025 - Email alerts (Milestone 3).

FR-3.1  Email every user of the tenant when a regulation is added.
FR-3.3  Email is the only alert channel.
FR-3.0  Content follows alert email template v1.1, signed off by Srinivas
        on Oct 19 2026 (T024).
FR-3.2  (T026) Email every user of the tenant when an existing regulation is
        updated.
DL-9    (Addendum A v1.7, Srinivas Oct 19 2026):
        - an "updated" email only when Admin/SME change the MAIN DETAILS
          (title, abbreviated title, due date, domain, country, region,
          Business Units, notes) - never for status, explanations or
          documents; switching "Add to Roadmap and Dashboard" from No to
          Yes sends the "New regulation" email, since that is when people
          can first see it;
        - each user can turn alerts off for themselves (on by default);
        - no email for regulations saved with "Add to Roadmap and
          Dashboard" = No, or for removals;
        - every email links into the app.

Flow (TAD Sec. 9, process 5.0). POST /regulations saves the record, then
hands send_regulation_added_alert() to FastAPI BackgroundTasks, so the save
returns before any email is sent and a slow or failing email provider
never blocks it. At first deploy the hand-off becomes a Pub/Sub
"regulation.added" event consumed by the Alerts Worker; the worker calls
the same function (schedule_regulation_added_alert is the only line that
changes - OPEN-11).

Provider: Brevo transactional email API (free plan: 300 emails a day for
the whole account). One API call per recipient, so no recipient ever sees
another's address.

Modes (ALERTS_MODE in .env):
  log  (default)  build the email and write the Alerts Log entry, send
                  nothing. Used by the test suite and Cloud Build.
  send            actually send through Brevo.
  off             do nothing at all.
Even in send mode, addresses on ALERTS_BLOCKED_DOMAINS (default: the two
test-tenant domains) are never emailed, so test accounts can't use up the
daily quota or bounce.

Every alert writes one entry to tenants/{tenantId}/alertsLog (D4): what was
sent, to whom (user IDs, not addresses), who opted out, and what failed.
Nothing here ever raises into the request: a failure is logged and recorded.
The Brevo API key is never logged or stored.
"""
import hashlib
import html
import logging
import os
import re
import time
from datetime import date, datetime, timezone
from typing import Callable, Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

BREVO_URL = "https://api.brevo.com/v3/smtp/email"
BREVO_TIMEOUT_SECONDS = 15
RETRY_DELAY_SECONDS = 2.0
MODES = ("off", "log", "send")
DEFAULT_MODE = "log"
DEFAULT_BLOCKED_DOMAINS = "testtenant.com,tenantb.com"

TEAL, NAVY = "#0D7377", "#0F172A"
FONT = "Inter, 'Segoe UI', Arial, Helvetica, sans-serif"
MONO = "'JetBrains Mono', Consolas, 'Courier New', monospace"

# Same three person-set statuses as core/regulation_routes.py; None = not set.
STATUS_META = {
    "compliant": ("Compliant", "#15803D", "#DCFCE7", "&#10003;"),
    "pending": ("Pending", "#B45309", "#FEF3C7", "&#8230;"),
    "action_needed": ("Action needed", "#B91C1C", "#FEE2E2", "!"),
    None: ("Not set", "#475569", "#F1F5F9", "&#8211;"),
}

ROLE_LABELS = {"admin": "Admin", "sme": "SME", "user": "User"}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class AlertSendError(RuntimeError):
    """Brevo refused or could not be reached, after the retry."""


# --- Settings ------------------------------------------------------------------

def alert_settings() -> dict:
    """Read from the environment on every call, so a change to .env needs only
    a server restart, and tests can set values per test."""
    mode = (os.environ.get("ALERTS_MODE") or DEFAULT_MODE).strip().lower()
    if mode not in MODES:
        logger.warning("ALERTS_MODE '%s' not recognised; using '%s'.", mode, DEFAULT_MODE)
        mode = DEFAULT_MODE
    blocked = os.environ.get("ALERTS_BLOCKED_DOMAINS", DEFAULT_BLOCKED_DOMAINS)
    return {
        "mode": mode,
        "api_key": (os.environ.get("BREVO_API_KEY") or "").strip(),
        "sender_email": (os.environ.get("ALERT_SENDER_EMAIL") or "").strip(),
        "sender_name": (os.environ.get("ALERT_SENDER_NAME") or "Regulator Alerts").strip(),
        "app_base_url": (os.environ.get("APP_BASE_URL") or "http://localhost:5173").strip().rstrip("/"),
        "blocked_domains": {d.strip().lower() for d in blocked.split(",") if d.strip()},
    }


# --- Small pure helpers --------------------------------------------------------

def _e(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _one_line(value: str) -> str:
    """Subjects and names must be one line (no header injection)."""
    return " ".join(str(value or "").split())


def format_due_date(value) -> str:
    """'2027-02-18' -> '18 Feb 2027'. Anything else is shown as stored."""
    text = str(value or "").strip()
    try:
        d = date.fromisoformat(text)
    except ValueError:
        return text or "Not specified"
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def format_when(dt: datetime) -> str:
    """Times are shown in UTC: a tenant spans time zones and has no zone setting."""
    dt = dt.astimezone(timezone.utc)
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year}, {dt:%H:%M} UTC"


def regulation_link(app_base_url: str, reg_id: str) -> str:
    """Opens the regulation's panel on the Dashboard (ComplianceDashboard ?reg=)."""
    return f"{app_base_url}/dashboard?reg={quote(reg_id, safe='')}"


def alert_settings_link(app_base_url: str) -> str:
    return f"{app_base_url}/account/email-alerts"


def _document_key(gcs_uri: str) -> str:
    # Same as core.regulation_routes.document_key. Copied, not imported, so
    # regulation_routes can import this module (T026) without a cycle.
    return hashlib.sha1(gcs_uri.encode("utf-8")).hexdigest()[:16]


def visible_document_count(reg: dict) -> int:
    hidden = reg.get("hiddenDocuments") or {}
    return sum(1 for d in reg.get("documents") or [] if _document_key(d.get("gcsUri", "")) not in hidden)


def select_recipients(users: list, prefs: dict) -> tuple:
    """
    users: directory records [{id, email, displayName, hidden?}] of ONE tenant.
    prefs: {uid: userPreferences doc}. emailAlerts False = turned off (DL-9);
           missing = on.
    Returns (recipients [{uid, email, name}], opted_out [uid], no_email [uid],
    removed [uid]). Hidden (removed, NFR-8) users never receive alerts; they
    are listed so the Alerts Log shows why someone wasn't emailed. One email
    per address.
    """
    recipients, opted_out, no_email, removed, seen = [], [], [], [], set()
    for u in users:
        uid = u.get("id")
        if u.get("hidden"):
            removed.append(uid)
            continue
        email = str(u.get("email") or "").strip()
        if not email or "@" not in email:
            no_email.append(uid)
            continue
        if (prefs.get(uid) or {}).get("emailAlerts") is False:
            opted_out.append(uid)
            continue
        if email.lower() in seen:
            continue
        seen.add(email.lower())
        recipients.append({"uid": uid, "email": email, "name": _one_line(u.get("displayName") or email)})
    return recipients, opted_out, no_email, removed


# --- Email content (template v1.1) ----------------------------------------------

def _status_chip(status) -> str:
    label, fg, bg, glyph = STATUS_META.get(status, STATUS_META[None])
    return (
        f'<span style="display:inline-block;padding:3px 10px;border-radius:999px;background:{bg};color:{fg};'
        f'font-weight:600;font-size:13px;line-height:18px;"><span aria-hidden="true">{glyph}</span>&nbsp;{label}</span>'
    )


def _field_rows(rows) -> str:
    out = []
    for label, value, mono in rows:
        style = f"font-family:{MONO};font-size:13px;" if mono else ""
        out.append(
            f'<tr><td style="padding:4px 12px 4px 0;color:#475569;font-size:14px;white-space:nowrap;'
            f'vertical-align:top;width:140px;">{_e(label)}</td>'
            f'<td style="padding:4px 0;color:{NAVY};font-size:14px;{style}">{_e(value)}</td></tr>'
        )
    return "".join(out)


def _changes_html(changes) -> str:
    if not changes:
        return ""
    head = "padding:0 12px 6px 0;font-size:12px;color:#64748B;text-transform:uppercase;letter-spacing:.04em;"
    rows = "".join(
        f'<tr><td style="padding:8px 12px 8px 0;border-top:1px solid #E2E8F0;font-size:14px;color:#475569;'
        f'vertical-align:top;width:140px;">{_e(field)}</td>'
        f'<td style="padding:8px 12px 8px 0;border-top:1px solid #E2E8F0;font-size:14px;color:#64748B;'
        f'vertical-align:top;"><span style="text-decoration:line-through;">{_e(before)}</span></td>'
        f'<td style="padding:8px 0;border-top:1px solid #E2E8F0;font-size:14px;color:{NAVY};font-weight:600;'
        f'vertical-align:top;">{_e(after)}</td></tr>'
        for field, before, after in changes
    )
    return (
        '<tr><td style="padding:24px 32px 0 32px;">'
        f'<h2 style="margin:0 0 8px 0;font-size:16px;line-height:22px;color:{NAVY};">What changed</h2>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td style="{head}">Field</td><td style="{head}">Before</td><td style="{head}padding-right:0;">Now</td></tr>'
        f"{rows}</table></td></tr>"
    )


def render_alert(
    kind: str,
    reg: dict,
    *,
    tenant_name: str,
    bu_labels: dict,
    actor: dict,
    app_base_url: str,
    when: datetime,
    changes: Optional[list] = None,
) -> dict:
    """
    Builds the email for one regulation event. kind: "added" (T025) or
    "updated" (T026, with `changes` = [(field label, before, now)]).
    Returns {subject, html, text}. Every value from the record is escaped.
    """
    added = kind == "added"
    label = "New regulation added" if added else "Regulation updated"
    title = _one_line(reg.get("title"))
    name = _one_line(reg.get("abbreviatedTitle")) or title
    due = format_due_date(reg.get("dueDate"))
    if added:
        subject = f"[Regulator] New regulation: {name} (due {due})"
    else:
        subject = f"[Regulator] Updated: {name}"
        if changes and len(changes) == 1:
            subject += f" — {str(changes[0][0]).lower()} changed"
        elif changes:
            subject += f" — {len(changes)} details changed"
    subject = _one_line(subject)[:200]

    units = list(reg.get("businessUnitIds") or [])
    unit_names = [f"{bu_labels[bu]} ({bu})" if bu_labels.get(bu) else bu for bu in units]
    docs = visible_document_count(reg)
    fields = [
        ("Due date", due, False),
        ("Domain", reg.get("domain") or "Not specified", False),
        ("Country", reg.get("country") or "Not specified", False),
        ("Region", reg.get("region") or "Not specified", False),
        ("Business units", ", ".join(unit_names) or "Not specified", False),
        ("Documents", f"{docs} attached" if docs else "None", False),
        ("Notes", reg.get("notes") or "None", False),
        ("Record ID", reg.get("id", ""), True),
    ]
    statuses = reg.get("buStatuses") or {}
    unit_status = []
    for bu, shown in zip(units, unit_names):
        entry = statuses.get(bu) or {}
        status = entry.get("status") if entry.get("status") in STATUS_META else None
        unit_status.append((shown, status, (entry.get("note") or "").strip()))

    actor_role = ROLE_LABELS.get(actor.get("role"), "")
    who = _one_line(actor.get("displayName") or actor.get("email") or "Someone")
    who_line = f"{'Added' if added else 'Updated'} by {who}{f' ({actor_role})' if actor_role else ''} · {format_when(when)}"
    link = regulation_link(app_base_url, reg.get("id", ""))
    settings_link = alert_settings_link(app_base_url)
    event = "added" if added else "updated"

    bu_html = "".join(
        f'<tr><td style="padding:8px 12px 8px 0;border-top:1px solid #E2E8F0;font-size:14px;color:{NAVY};'
        f'vertical-align:top;width:180px;">{_e(shown)}</td>'
        f'<td style="padding:8px 0;border-top:1px solid #E2E8F0;vertical-align:top;">{_status_chip(status)}'
        + (f'<div style="color:#475569;font-size:13px;padding-top:4px;">{_e(note)}</div>' if note else "")
        + "</td></tr>"
        for shown, status, note in unit_status
    )

    html_body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light only"><title>{_e(subject)}</title></head>
<body style="margin:0;padding:0;background:#EEF2F6;font-family:{FONT};">
<div style="display:none;max-height:0;overflow:hidden;">{_e(title)} &middot; due {_e(due)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#EEF2F6;">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;background:#FFFFFF;border:1px solid #E2E8F0;border-radius:12px;">
<tr><td style="padding:24px 32px 16px 32px;border-bottom:1px solid #E2E8F0;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
<td style="width:6px;background:{TEAL};border-radius:3px;">&nbsp;</td>
<td style="padding-left:12px;"><div style="font-size:22px;line-height:28px;font-weight:700;color:{NAVY};">Regulator</div>
<div style="font-size:13px;line-height:18px;color:#475569;">by Sustainer &middot; {_e(label)}</div></td></tr></table>
</td></tr>
<tr><td style="padding:24px 32px 0 32px;">
<div style="font-size:12px;line-height:16px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:{TEAL};">{_e(label)}</div>
<h1 style="margin:6px 0 2px 0;font-size:20px;line-height:26px;color:{NAVY};">{_e(name)}</h1>
{f'<div style="font-size:15px;line-height:22px;color:#334155;">{_e(title)}</div>' if name != title else ''}
<div style="font-size:13px;line-height:20px;color:#475569;padding-top:6px;">{_e(who_line)}</div>
</td></tr>
{_changes_html(changes)}
<tr><td style="padding:24px 32px 0 32px;">
<h2 style="margin:0 0 8px 0;font-size:16px;line-height:22px;color:{NAVY};">Details</h2>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{_field_rows(fields)}</table>
</td></tr>
<tr><td style="padding:24px 32px 0 32px;">
<h2 style="margin:0 0 4px 0;font-size:16px;line-height:22px;color:{NAVY};">Status by Business Unit</h2>
<div style="font-size:13px;line-height:20px;color:#475569;padding-bottom:6px;">Set by people on the Dashboard, never by AI.</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{bu_html}</table>
</td></tr>
<tr><td style="padding:28px 32px 8px 32px;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr><td style="background:{NAVY};border-radius:8px;">
<a href="{_e(link)}" style="display:inline-block;padding:13px 28px;font-size:15px;font-weight:600;color:#FFFFFF;text-decoration:none;">View in Regulator</a>
</td></tr></table>
<div style="font-size:12px;line-height:18px;color:#64748B;padding-top:8px;">Opens this regulation on the Dashboard. You may be asked to sign in.<br>
If the button doesn&rsquo;t work, open: <a href="{_e(link)}" style="color:{TEAL};">{_e(link)}</a></div>
</td></tr>
<tr><td style="padding:20px 32px 28px 32px;">
<div style="border-top:1px solid #E2E8F0;padding-top:16px;font-size:12px;line-height:18px;color:#64748B;">
Sent to every user at {_e(tenant_name)} with email alerts on, when a regulation is {event}. Regulator alerts are sent by email only.<br>
Don&rsquo;t want these emails? <a href="{_e(settings_link)}" style="color:{TEAL};">Turn off email alerts</a> in Regulator (you can turn them back on at any time).<br>
This is an automated message &mdash; replies are not monitored.
</div></td></tr>
</table></td></tr></table>
</body></html>"""

    lines = [label.upper(), "", name]
    if name != title:
        lines.append(title)
    lines += [who_line, ""]
    if changes:
        lines.append("What changed:")
        lines += [f"- {f}: {_one_line(b)} -> {_one_line(a)}" for f, b, a in changes]
        lines.append("")
    lines.append("Details:")
    lines += [f"- {k}: {_one_line(v)}" for k, v, _ in fields]
    lines += ["", "Status by Business Unit (set by people, never by AI):"]
    lines += [
        f"- {shown}: {STATUS_META[status][0]}" + (f" - {_one_line(note)}" if note else "")
        for shown, status, note in unit_status
    ]
    lines += [
        "",
        f"View in Regulator: {link}",
        "",
        f"Sent to every user at {tenant_name} with email alerts on, when a regulation is {event}.",
        f"Turn off email alerts: {settings_link}",
        "This is an automated message - replies are not monitored.",
    ]
    return {"subject": subject, "html": html_body, "text": "\n".join(lines)}


# --- Sending -------------------------------------------------------------------

_ADDRESS_RE = re.compile(r"[^\s@<>\"',;:()\[\]]+@[^\s@<>\"',;:()\[\]]+")


def _redact(text: str) -> str:
    """T027: Brevo's error text can quote the recipient's address ("... is not
    valid"); the Alerts Log keeps user IDs only, never addresses."""
    return _ADDRESS_RE.sub("[address]", text or "")


def _brevo_error(resp) -> str:
    try:
        message = (resp.json() or {}).get("message") or ""
    except ValueError:
        message = ""
    message = _redact(message)
    return f"HTTP {resp.status_code}{': ' + message[:200] if message else ''}"


def send_one_brevo(post: Callable, cfg: dict, recipient: dict, message: dict) -> str:
    """One email to one person. Retries once on a dropped connection, a 5xx
    or a 429. Returns Brevo's message ID; raises AlertSendError otherwise."""
    payload = {
        "sender": {"name": cfg["sender_name"], "email": cfg["sender_email"]},
        "to": [{"email": recipient["email"], "name": recipient["name"]}],
        "subject": message["subject"],
        "htmlContent": message["html"],
        "textContent": message["text"],
        "tags": ["regulator-alert"],
    }
    headers = {"api-key": cfg["api_key"], "accept": "application/json", "content-type": "application/json"}
    last_error = ""
    for attempt in (1, 2):
        try:
            resp = post(BREVO_URL, json=payload, headers=headers, timeout=BREVO_TIMEOUT_SECONDS)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = type(e).__name__
        else:
            if resp.status_code in (200, 201, 202):
                try:
                    body = resp.json() or {}
                except ValueError:
                    body = {}
                return body.get("messageId") or ",".join(body.get("messageIds") or []) or ""
            last_error = _brevo_error(resp)
            if resp.status_code < 500 and resp.status_code != 429:
                break  # a real refusal (bad key, bad sender): retrying won't help
        if attempt == 1:
            time.sleep(RETRY_DELAY_SECONDS)
    raise AlertSendError(last_error)


def deliver(cfg: dict, recipients: list, message: dict, post: Optional[Callable] = None) -> dict:
    """Sends (or, in log mode, doesn't) and reports what happened per person."""
    result = {"status": "logged", "sent": [], "failed": [], "skipped_test_domain": [], "message_ids": []}
    if not recipients:
        result["status"] = "no_recipients"
        return result
    if cfg["mode"] != "send":
        return result
    if not cfg["api_key"] or not cfg["sender_email"]:
        result["status"] = "failed"
        result["failed"] = [{"uid": r["uid"], "error": "BREVO_API_KEY or ALERT_SENDER_EMAIL not set"} for r in recipients]
        return result

    post = post or requests.post
    for r in recipients:
        domain = r["email"].rsplit("@", 1)[-1].lower()
        if domain in cfg["blocked_domains"]:
            result["skipped_test_domain"].append(r["uid"])
            continue
        try:
            result["message_ids"].append(send_one_brevo(post, cfg, r, message))
            result["sent"].append(r["uid"])
        except AlertSendError as e:
            result["failed"].append({"uid": r["uid"], "error": str(e)})

    if result["failed"]:
        result["status"] = "partly_sent" if result["sent"] else "failed"
    elif result["sent"]:
        result["status"] = "sent"
    else:
        result["status"] = "no_recipients"  # everyone was on a test domain
    return result


# --- Orchestration -------------------------------------------------------------

def _tenant_context(tenant_ref, actor_uid: str):
    tenant_snap = tenant_ref.get()
    tenant_name = (tenant_snap.to_dict() or {}).get("name") if tenant_snap.exists else None
    bu_labels = {}
    for doc in tenant_ref.collection("businessUnits").stream():
        data = doc.to_dict() or {}
        bu_labels[data.get("code") or doc.id] = data.get("label") or ""
    users = [{"id": doc.id, **(doc.to_dict() or {})} for doc in tenant_ref.collection("users").stream()]
    prefs = {doc.id: (doc.to_dict() or {}) for doc in tenant_ref.collection("userPreferences").stream()}
    actor = next((u for u in users if u["id"] == actor_uid), {})
    return tenant_name or tenant_ref.id, bu_labels, users, prefs, actor


# --- What counts as an update (T026, DL-9) ---------------------------------------

# Stored field -> label. The main details from Srinivas's Add Regulation form;
# changing any of these (Admin/SME, PATCH /regulations/{id}) emails everyone.
MAIN_DETAIL_FIELDS = (
    ("title", "Title"),
    ("abbreviatedTitle", "Abbreviated title"),
    ("dueDate", "Due date"),
    ("domain", "Domain"),
    ("country", "Country"),
    ("region", "Region"),
    ("businessUnitIds", "Business units"),
    ("notes", "Notes"),
)
FIELD_LABELS = dict(MAIN_DETAIL_FIELDS)
CHANGE_TEXT_MAX = 300


def _comparable(value):
    if isinstance(value, (list, tuple)):
        return sorted(str(v).strip() for v in value)
    return str(value if value is not None else "").strip()


def plan_update_alert(before: dict, update: dict) -> tuple:
    """
    Decides, from the record before a PATCH and the fields it wrote, which
    email (if any) the edit should send. Pure, so it is tested on its own.
    Returns ("added", []), ("updated", [[field, before, after], ...]) or (None, []).
      - removed, or not on the Roadmap/Dashboard after the edit: nothing (DL-9);
      - "Add to Roadmap and Dashboard" switched No -> Yes: "added";
      - otherwise "updated" if any main detail really changed (same value
        re-sent, or only a reordering of Business Units, is not a change).
    """
    if before.get("hidden"):
        return None, []
    was_on = before.get("addToRoadmapDashboard") is True
    now_on = update.get("addToRoadmapDashboard", before.get("addToRoadmapDashboard")) is True
    if not now_on:
        return None, []
    if not was_on:
        return "added", []
    changes = [
        [field, before.get(field), update[field]]
        for field, _ in MAIN_DETAIL_FIELDS
        if field in update and _comparable(update[field]) != _comparable(before.get(field))
    ]
    return ("updated", changes) if changes else (None, [])


def format_change_value(field: str, value, bu_labels: dict) -> str:
    if field == "dueDate":
        return format_due_date(value)
    if field == "businessUnitIds":
        units = [f"{bu_labels[bu]} ({bu})" if bu_labels.get(bu) else str(bu) for bu in (value or [])]
        return ", ".join(units) or "Not specified"
    text = _one_line(value)
    if not text:
        return "Not specified"
    return text if len(text) <= CHANGE_TEXT_MAX else text[: CHANGE_TEXT_MAX - 1] + "…"


# --- Orchestration -------------------------------------------------------------

def _send_alert(kind: str, tenant_id: str, reg_id: str, actor_uid: str,
                changes: Optional[list] = None, db=None, post=None) -> Optional[dict]:
    """
    Sends one alert about one regulation and writes its Alerts Log entry.
    kind: "added" (FR-3.1) or "updated" (FR-3.2, with raw `changes` from
    plan_update_alert). Runs after the response. Returns the entry, or None
    when no alert applies. Never raises.
    """
    alert_type = "regulation_added" if kind == "added" else "regulation_updated"
    cfg = alert_settings()
    if cfg["mode"] == "off":
        return None
    try:
        if db is None:
            from core.deps import db  # lazy: keeps this module importable without credentials
        tenant_ref = db.collection("tenants").document(tenant_id)
        snap = tenant_ref.collection("regulations").document(reg_id).get()
        if not snap.exists:
            logger.warning("Alert: regulation %s not found in %s; nothing sent.", reg_id, tenant_id)
            return None
        reg = {"id": reg_id, **(snap.to_dict() or {})}
        # DL-9: no email for regulations not on the Roadmap/Dashboard, or removed ones.
        if reg.get("hidden") or reg.get("addToRoadmapDashboard") is not True:
            logger.info("Alert: regulation %s is not on the Dashboard; no email (DL-9).", reg_id)
            return None

        tenant_name, bu_labels, users, prefs, actor = _tenant_context(tenant_ref, actor_uid)
        recipients, opted_out, no_email, removed = select_recipients(users, prefs)
        rows = [
            (FIELD_LABELS.get(field, field),
             format_change_value(field, before, bu_labels),
             format_change_value(field, after, bu_labels))
            for field, before, after in (changes or [])
        ]
        now = datetime.now(timezone.utc)
        message = render_alert(
            kind, reg, tenant_name=tenant_name, bu_labels=bu_labels, actor=actor,
            app_base_url=cfg["app_base_url"], when=now, changes=rows or None,
        )
        outcome = deliver(cfg, recipients, message, post=post)

        entry = {
            "type": alert_type,
            "regulationId": reg_id,
            "regulationTitle": reg.get("title", ""),
            "subject": message["subject"],
            "triggeredBy": actor_uid,
            "mode": cfg["mode"],
            "status": outcome["status"],
            "recipientUids": [r["uid"] for r in recipients],
            "recipientCount": len(recipients),
            "sentUids": outcome["sent"],
            "sentCount": len(outcome["sent"]),
            "failed": outcome["failed"],
            "skippedOptedOut": opted_out,
            "skippedNoEmail": no_email,
            "skippedRemoved": removed,
            "skippedTestDomain": outcome["skipped_test_domain"],
            "providerMessageIds": outcome["message_ids"],
            "createdAt": now,
        }
        if kind == "updated":
            # Field names only; the before/after values are in the audit log (NFR-2).
            entry["changedFields"] = [field for field, _, _ in (changes or [])]
        tenant_ref.collection("alertsLog").document().set(entry)
        logger.info(
            "Alert: %s %s -> %s (%d recipients, %d sent, %d failed, %d opted out, mode=%s)",
            alert_type, reg_id, outcome["status"], len(recipients), len(outcome["sent"]),
            len(outcome["failed"]), len(opted_out), cfg["mode"],
        )
        print(f"[alert] {alert_type} {reg_id}: {outcome['status']} ({len(outcome['sent'])} sent, mode={cfg['mode']})")
        return entry
    except Exception as e:  # noqa: BLE001 - an alert must never break anything else
        logger.exception("Alert: %s for %s failed: %s", alert_type, reg_id, type(e).__name__)
        try:
            db.collection("tenants").document(tenant_id).collection("alertsLog").document().set({
                "type": alert_type,
                "regulationId": reg_id,
                "triggeredBy": actor_uid,
                "mode": cfg["mode"],
                "status": "failed",
                "error": type(e).__name__,
                "createdAt": datetime.now(timezone.utc),
            })
        except Exception:  # noqa: BLE001
            pass
        return None


def send_regulation_added_alert(tenant_id: str, reg_id: str, actor_uid: str, db=None, post=None) -> Optional[dict]:
    """FR-3.1: a regulation was added to the Roadmap and Dashboard."""
    return _send_alert("added", tenant_id, reg_id, actor_uid, db=db, post=post)


def send_regulation_updated_alert(tenant_id: str, reg_id: str, actor_uid: str, changes: list,
                                  db=None, post=None) -> Optional[dict]:
    """FR-3.2: Admin/SME changed the main details of a regulation on the Dashboard."""
    return _send_alert("updated", tenant_id, reg_id, actor_uid, changes=changes, db=db, post=post)


def schedule_regulation_added_alert(background_tasks, tenant_id: str, reg_id: str, actor_uid: str) -> None:
    """The hand-off point. Today: run after the response (BackgroundTasks).
    At first deploy: publish a regulation.added Pub/Sub event instead (OPEN-11)."""
    background_tasks.add_task(send_regulation_added_alert, tenant_id, reg_id, actor_uid)


def schedule_regulation_updated_alert(background_tasks, tenant_id: str, reg_id: str, actor_uid: str,
                                      before: dict, update: dict) -> Optional[str]:
    """
    T026 hand-off from PATCH /regulations/{id}. Decides now, from the record
    before the edit and the fields written, whether an email applies (see
    plan_update_alert), and schedules it after the response. Returns the kind
    scheduled ("added" / "updated") or None. At first deploy: publish a
    regulation.updated Pub/Sub event instead (OPEN-11).
    """
    kind, changes = plan_update_alert(before, update)
    if kind == "added":
        background_tasks.add_task(send_regulation_added_alert, tenant_id, reg_id, actor_uid)
    elif kind == "updated":
        background_tasks.add_task(send_regulation_updated_alert, tenant_id, reg_id, actor_uid, changes)
    return kind


# --- Turning alerts back on (T026) -------------------------------------------------

BREVO_BLOCKED_URL = "https://api.brevo.com/v3/smtp/blockedContacts/"


def resubscribe_at_provider(email: str, delete: Optional[Callable] = None) -> str:
    """
    Brevo adds a List-Unsubscribe header to every email (removable on its
    Enterprise plan only), so Gmail and Outlook show their own "Unsubscribe"
    link. Using it blocks the address at Brevo, outside the app. When the
    person turns alerts back ON in Regulator, this lifts that block so the
    app's setting is what decides. Send mode only. Never raises.
    Returns "unblocked", "not_blocked", "skipped" or "failed".
    """
    cfg = alert_settings()
    email = str(email or "").strip()
    if cfg["mode"] != "send" or not cfg["api_key"] or "@" not in email:
        return "skipped"
    delete = delete or requests.delete
    try:
        resp = delete(
            BREVO_BLOCKED_URL + quote(email, safe=""),
            headers={"api-key": cfg["api_key"], "accept": "application/json"},
            timeout=BREVO_TIMEOUT_SECONDS,
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        logger.warning("Alert: could not reach Brevo to lift a block (%s).", type(e).__name__)
        return "failed"
    if resp.status_code == 204:
        logger.info("Alert: lifted a Brevo block after alerts were turned back on.")
        return "unblocked"
    if resp.status_code == 404:
        return "not_blocked"
    logger.warning("Alert: Brevo refused to lift a block: %s", _brevo_error(resp))
    return "failed"
