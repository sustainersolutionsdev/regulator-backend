"""
core/dispatch.py - how alert work leaves the API request (OPEN-11 point 6).

ALERTS_DISPATCH (environment):
  inline  (default)  FastAPI BackgroundTasks in the API process, exactly as
                     in M3. Used locally and by the Cloud Build test suite.
  pubsub             publish one event to the ALERTS_TOPIC Pub/Sub topic;
                     the Alerts Worker (worker.py, its own Cloud Run service)
                     receives it and does the work (TAD Sec. 9).

Why the deployed API must use pubsub: Cloud Run only guarantees CPU while a
request is being handled. Work queued with BackgroundTasks runs after the
response, when the instance may be throttled or shut down, so an alert email
could silently never go out. Pub/Sub hands the work to a separate request on
the worker, which Pub/Sub retries if it fails.

Events (JSON, no email addresses or other personal data - the worker looks
people up itself):
  {"type": "regulation.added",   "tenantId", "regulationId", "actorUid"}
  {"type": "regulation.updated", "tenantId", "regulationId", "actorUid", "changes"}
  {"type": "alerts.resubscribe", "tenantId", "uid"}
  {"type": "user.invited",       "tenantId", "uid", "actorUid", "resend"}   (T061)
"""
import json
import logging
import os
from typing import Optional

from core.config import GCP_PROJECT

logger = logging.getLogger(__name__)

DISPATCH_MODES = {"inline", "pubsub"}
DEFAULT_TOPIC = "regulator-alerts"
PUBLISH_TIMEOUT_SECONDS = 10

_publisher = None


def dispatch_mode() -> str:
    mode = (os.environ.get("ALERTS_DISPATCH") or "inline").strip().lower()
    if mode not in DISPATCH_MODES:
        logger.warning("ALERTS_DISPATCH '%s' not recognised; using 'inline'.", mode)
        return "inline"
    return mode


def topic_path() -> str:
    topic = (os.environ.get("ALERTS_TOPIC") or DEFAULT_TOPIC).strip()
    return f"projects/{GCP_PROJECT}/topics/{topic}"


def _get_publisher():
    global _publisher
    if _publisher is None:
        from google.cloud import pubsub_v1  # imported only where Pub/Sub is used
        _publisher = pubsub_v1.PublisherClient()
    return _publisher


def encode_event(event: dict) -> bytes:
    # default=str: dates or timestamps inside "changes" travel as text, which is
    # all the email needs (format_change_value prints them).
    return json.dumps(event, default=str, separators=(",", ":")).encode("utf-8")


def publish_event(event: dict, publisher=None) -> Optional[str]:
    """Publish one event and wait for Pub/Sub to accept it, so the event is
    safe before the API responds. Returns the message ID, or None on failure
    (logged, never raised: an alert must never fail a save)."""
    try:
        pub = publisher or _get_publisher()
        future = pub.publish(topic_path(), encode_event(event), type=str(event.get("type", "")))
        message_id = future.result(timeout=PUBLISH_TIMEOUT_SECONDS)
        logger.info("Alert event %s published (%s).", event.get("type"), message_id)
        return message_id
    except Exception as e:  # noqa: BLE001
        logger.exception("Alert event %s could not be published: %s", event.get("type"), type(e).__name__)
        return None
