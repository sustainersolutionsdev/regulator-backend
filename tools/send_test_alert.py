"""
T025 - send ONE real "New regulation added" email through Brevo, to one
address you choose, for an existing regulation. Checks the Brevo key, the
verified sender and how the email looks in a real inbox, without emailing
anyone else and without writing an Alerts Log entry.

Run from regulator-backend:
    python tools\\send_test_alert.py --to you@example.com
    python tools\\send_test_alert.py --to you@example.com --reg <regulation id>

Without --reg it uses the most recently added regulation on the Dashboard
in test-tenant-001. Uses BREVO_API_KEY, ALERT_SENDER_EMAIL,
ALERT_SENDER_NAME and APP_BASE_URL from .env; ALERTS_MODE is ignored here.
Counts as 1 of the free plan's 300 emails a day.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from datetime import datetime, timezone

import requests

from core.alerts import AlertSendError, _tenant_context, alert_settings, render_alert, send_one_brevo
from core.deps import db

TENANT = "test-tenant-001"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", required=True, help="the one address to send to")
    parser.add_argument("--reg", help="regulation id (default: newest on the Dashboard)")
    args = parser.parse_args()

    cfg = alert_settings()
    if not cfg["api_key"] or not cfg["sender_email"]:
        print("BREVO_API_KEY and ALERT_SENDER_EMAIL must be set in .env.")
        return 1

    tenant_ref = db.collection("tenants").document(TENANT)
    if args.reg:
        snap = tenant_ref.collection("regulations").document(args.reg).get()
        if not snap.exists:
            print(f"Regulation {args.reg} not found in {TENANT}.")
            return 1
        reg = {"id": snap.id, **snap.to_dict()}
    else:
        regs = [
            {"id": d.id, **d.to_dict()}
            for d in tenant_ref.collection("regulations").stream()
        ]
        regs = [r for r in regs if r.get("addToRoadmapDashboard") is True and not r.get("hidden")]
        if not regs:
            print(f"No regulation on the Dashboard in {TENANT}. Add one first.")
            return 1
        reg = max(regs, key=lambda r: r.get("createdAt") or datetime.min.replace(tzinfo=timezone.utc))

    tenant_name, bu_labels, _, _, actor = _tenant_context(tenant_ref, reg.get("createdBy", ""))
    message = render_alert(
        "added", reg, tenant_name=tenant_name, bu_labels=bu_labels, actor=actor,
        app_base_url=cfg["app_base_url"], when=datetime.now(timezone.utc),
    )
    print(f"Regulation: {reg.get('title')} ({reg['id']})")
    print(f"Subject:    {message['subject']}")
    print(f"From:       {cfg['sender_name']} <{cfg['sender_email']}>")
    print(f"To:         {args.to}")
    try:
        message_id = send_one_brevo(requests.post, cfg, {"uid": "-", "email": args.to, "name": args.to}, message)
    except AlertSendError as e:
        print(f"NOT SENT - Brevo answered: {e}")
        return 1
    print(f"Sent. Brevo message id: {message_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
