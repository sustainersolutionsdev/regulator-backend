"""
worker.py - the Alerts Worker (TAD Sec. 9, DFD process 5.0; OPEN-11 point 6).

A small FastAPI app, deployed as its own Cloud Run service
(regulator-alerts-worker) from the same container image as the API, so both
share core/ and there is one build. A Pub/Sub push subscription on the
regulator-alerts topic posts each event to /pubsub/alerts.

Security: the service is deployed with --no-allow-unauthenticated. Only the
push subscription's service account (roles/run.invoker) can call it; Cloud
Run checks that before any code here runs. The worker holds the Brevo key;
the API no longer needs it.

Delivery is at-least-once, so the same event can arrive twice. Each
regulation event is recorded by its Pub/Sub message ID under
tenants/{t}/alertEvents/{messageId} before sending; a repeat is skipped, so
nobody gets the same email twice.

Responses: 204 when handled (or skipped as a repeat or bad message, so
Pub/Sub doesn't retry something that can never work); 500 only for an
unexpected error, which Pub/Sub retries. The send functions themselves never
raise - their failures are recorded in the Alerts Log (D4).
"""
import base64
import json
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("alerts-worker")

app = FastAPI(title="Regulator Alerts Worker", docs_url=None, redoc_url=None, openapi_url=None)

KNOWN_TYPES = {"regulation.added", "regulation.updated", "alerts.resubscribe"}


def decode_push(envelope: dict) -> tuple[str, dict]:
    """Pub/Sub push body -> (message ID, event). Raises ValueError if malformed."""
    message = (envelope or {}).get("message") or {}
    message_id = str(message.get("messageId") or message.get("message_id") or "")
    data = message.get("data")
    if not message_id or not data:
        raise ValueError("push message without messageId or data")
    event = json.loads(base64.b64decode(data).decode("utf-8"))
    if not isinstance(event, dict) or event.get("type") not in KNOWN_TYPES:
        raise ValueError(f"unknown event type: {event.get('type') if isinstance(event, dict) else type(event)}")
    if not event.get("tenantId"):
        raise ValueError("event without tenantId")
    return message_id, event


def claim_message(db, tenant_id: str, message_id: str, event_type: str) -> bool:
    """True the first time a message ID is seen for this tenant, False on a repeat."""
    from google.api_core.exceptions import AlreadyExists

    ref = db.collection("tenants").document(tenant_id).collection("alertEvents").document(message_id)
    try:
        ref.create({"type": event_type, "receivedAt": datetime.now(timezone.utc)})
        return True
    except AlreadyExists:
        return False


def handle_event(message_id: str, event: dict, db=None) -> str:
    """Runs one event. Returns what happened (for the log and tests)."""
    from core import alerts

    if db is None:
        from core.deps import db  # lazy: needs credentials only when actually used
    tenant_id = event["tenantId"]
    kind = event["type"]

    if kind == "alerts.resubscribe":
        uid = event.get("uid")
        snap = db.collection("tenants").document(tenant_id).collection("users").document(uid).get() if uid else None
        email = (snap.to_dict() or {}).get("email") if snap is not None and snap.exists else None
        return alerts.resubscribe_at_provider(email) if email else "skipped"

    if not claim_message(db, tenant_id, message_id, kind):
        logger.info("Event %s already handled; skipped.", message_id)
        return "duplicate"
    if kind == "regulation.added":
        alerts.send_regulation_added_alert(tenant_id, event["regulationId"], event.get("actorUid", ""), db=db)
        return "sent_added"
    alerts.send_regulation_updated_alert(
        tenant_id, event["regulationId"], event.get("actorUid", ""),
        [tuple(c) for c in (event.get("changes") or [])], db=db,
    )
    return "sent_updated"


@app.post("/pubsub/alerts")
async def pubsub_alerts(request: Request):
    try:
        envelope = await request.json()
        message_id, event = decode_push(envelope)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as e:
        # A malformed message will never succeed: acknowledge it, don't retry forever.
        logger.error("Dropped malformed alert event: %s", e)
        return Response(status_code=204)
    try:
        outcome = handle_event(message_id, event)
        logger.info("Event %s (%s) -> %s", message_id, event["type"], outcome)
        return Response(status_code=204)
    except Exception as e:  # noqa: BLE001
        logger.exception("Event %s failed: %s", message_id, type(e).__name__)
        return Response(status_code=500)


@app.get("/health")  # not /healthz: Cloud Run reserves some paths ending in "z"
def health():
    return {"status": "ok"}
