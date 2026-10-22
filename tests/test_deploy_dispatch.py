"""
OPEN-11 (first deploy): per-environment config, the Pub/Sub alert hand-off and
the Alerts Worker. No server, Firestore or Pub/Sub needed - fakes throughout,
like test_alerts_unit.py.
"""
import base64
import datetime as dt
import importlib
import json

import pytest
from fastapi.testclient import TestClient

from core import alerts, dispatch


# --- config -------------------------------------------------------------------

def test_config_defaults_match_local_dev(monkeypatch):
    for name in ("GCP_PROJECT", "DOCUMENTS_BUCKET", "CORS_ORIGINS"):
        monkeypatch.delenv(name, raising=False)
    import core.config as config
    config = importlib.reload(config)
    assert config.GCP_PROJECT == "regulator-dev"
    assert config.DOCUMENTS_BUCKET == "regulator-dev-documents"
    assert config.cors_origins() == ["http://localhost:5173"]


def test_config_reads_environment(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT", "regulator-beta")
    monkeypatch.delenv("DOCUMENTS_BUCKET", raising=False)
    monkeypatch.setenv("CORS_ORIGINS", " https://regulator-beta.web.app/ , http://localhost:5173,, ")
    import core.config as config
    config = importlib.reload(config)
    try:
        assert config.GCP_PROJECT == "regulator-beta"
        assert config.DOCUMENTS_BUCKET == "regulator-beta-documents"
        assert config.cors_origins() == ["https://regulator-beta.web.app", "http://localhost:5173"]
    finally:
        monkeypatch.delenv("GCP_PROJECT")
        monkeypatch.delenv("CORS_ORIGINS")
        importlib.reload(config)


# --- dispatch -----------------------------------------------------------------

class FakeTasks:
    def __init__(self):
        self.calls = []

    def add_task(self, fn, *args):
        self.calls.append((fn, args))


class FakeFuture:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error

    def result(self, timeout=None):
        if self.error:
            raise self.error
        return self.value


class FakePublisher:
    def __init__(self, error=None):
        self.published = []
        self.error = error

    def publish(self, topic, data, **attrs):
        self.published.append((topic, json.loads(data.decode()), attrs))
        return FakeFuture("msg-1", self.error)


@pytest.fixture
def publisher(monkeypatch):
    pub = FakePublisher()
    monkeypatch.setattr(dispatch, "_publisher", pub)
    return pub


ON = {"title": "Old title", "addToRoadmapDashboard": True}


def test_inline_mode_keeps_m3_behaviour(monkeypatch, publisher):
    monkeypatch.delenv("ALERTS_DISPATCH", raising=False)
    bg = FakeTasks()
    alerts.schedule_regulation_added_alert(bg, "t1", "r1", "u1")
    assert bg.calls == [(alerts.send_regulation_added_alert, ("t1", "r1", "u1"))]
    assert publisher.published == []


def test_pubsub_mode_publishes_added_event(monkeypatch, publisher):
    monkeypatch.setenv("ALERTS_DISPATCH", "pubsub")
    monkeypatch.setenv("ALERTS_TOPIC", "regulator-alerts")
    bg = FakeTasks()
    alerts.schedule_regulation_added_alert(bg, "t1", "r1", "u1")
    assert bg.calls == []
    topic, event, attrs = publisher.published[0]
    assert topic.endswith("/topics/regulator-alerts") and topic.startswith("projects/")
    assert event == {"type": "regulation.added", "tenantId": "t1", "regulationId": "r1", "actorUid": "u1"}
    assert attrs == {"type": "regulation.added"}


def test_pubsub_mode_publishes_updated_event_with_changes(monkeypatch, publisher):
    monkeypatch.setenv("ALERTS_DISPATCH", "pubsub")
    bg = FakeTasks()
    kind = alerts.schedule_regulation_updated_alert(bg, "t1", "r1", "u1", ON, {"title": "New title"})
    assert kind == "updated" and bg.calls == []
    event = publisher.published[0][1]
    assert event["type"] == "regulation.updated"
    assert event["changes"] == [["title", "Old title", "New title"]]
    assert "email" not in json.dumps(event).lower()  # no addresses in events


def test_pubsub_mode_switch_onto_dashboard_publishes_added(monkeypatch, publisher):
    monkeypatch.setenv("ALERTS_DISPATCH", "pubsub")
    kind = alerts.schedule_regulation_updated_alert(
        FakeTasks(), "t1", "r1", "u1", {**ON, "addToRoadmapDashboard": False}, {"addToRoadmapDashboard": True})
    assert kind == "added" and publisher.published[0][1]["type"] == "regulation.added"


def test_publish_failure_falls_back_to_in_process(monkeypatch):
    monkeypatch.setenv("ALERTS_DISPATCH", "pubsub")
    monkeypatch.setattr(dispatch, "_publisher", FakePublisher(error=TimeoutError("pubsub down")))
    bg = FakeTasks()
    alerts.schedule_regulation_added_alert(bg, "t1", "r1", "u1")
    assert bg.calls == [(alerts.send_regulation_added_alert, ("t1", "r1", "u1"))]


def test_unknown_dispatch_mode_falls_back_to_inline(monkeypatch, publisher):
    monkeypatch.setenv("ALERTS_DISPATCH", "carrier-pigeon")
    assert dispatch.dispatch_mode() == "inline"


def test_event_encoding_handles_dates():
    data = dispatch.encode_event({"type": "regulation.updated", "changes": [["dueDate", dt.date(2026, 1, 2), None]]})
    assert json.loads(data)["changes"][0][1] == "2026-01-02"


# --- worker -------------------------------------------------------------------

import worker  # noqa: E402


def push_body(event, message_id="m-1"):
    data = base64.b64encode(json.dumps(event).encode()).decode()
    return {"message": {"data": data, "messageId": message_id}, "subscription": "projects/p/subscriptions/s"}


class FakeDoc:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def collection(self, name):
        return FakeCol(self.store, self.path + (name,))

    def document(self, name):  # pragma: no cover - not used on documents
        raise AttributeError

    def create(self, data):
        from google.api_core.exceptions import AlreadyExists
        if self.path in self.store:
            raise AlreadyExists("exists")
        self.store[self.path] = data

    def get(self):
        data = self.store.get(self.path)
        return type("Snap", (), {"exists": data is not None, "to_dict": lambda self_: data})()


class FakeCol:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def document(self, name):
        return FakeDoc(self.store, self.path + (name,))


class FakeDb:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return FakeCol(self.store, (name,))


def test_decode_push_rejects_bad_messages():
    with pytest.raises(ValueError):
        worker.decode_push({})
    with pytest.raises(ValueError):
        worker.decode_push(push_body({"type": "something.else", "tenantId": "t1"}))
    with pytest.raises(ValueError):
        worker.decode_push(push_body({"type": "regulation.added"}))
    mid, event = worker.decode_push(push_body({"type": "regulation.added", "tenantId": "t1", "regulationId": "r1"}))
    assert mid == "m-1" and event["regulationId"] == "r1"


def test_handle_event_sends_once_per_message(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "send_regulation_added_alert", lambda *a, **k: sent.append(a))
    db = FakeDb()
    event = {"type": "regulation.added", "tenantId": "t1", "regulationId": "r1", "actorUid": "u1"}
    assert worker.handle_event("m-1", event, db=db) == "sent_added"
    assert worker.handle_event("m-1", event, db=db) == "duplicate"   # Pub/Sub redelivery
    assert worker.handle_event("m-2", event, db=db) == "sent_added"  # a genuinely new event
    assert sent == [("t1", "r1", "u1"), ("t1", "r1", "u1")]


def test_handle_event_updated_passes_changes_as_tuples(monkeypatch):
    got = {}
    monkeypatch.setattr(alerts, "send_regulation_updated_alert",
                        lambda t, r, u, changes, db=None: got.update(changes=changes))
    event = {"type": "regulation.updated", "tenantId": "t1", "regulationId": "r1", "actorUid": "u1",
             "changes": [["title", "A", "B"]]}
    assert worker.handle_event("m-9", event, db=FakeDb()) == "sent_updated"
    assert got["changes"] == [("title", "A", "B")]


def test_handle_event_resubscribe_looks_up_address(monkeypatch):
    calls = []
    monkeypatch.setattr(alerts, "resubscribe_at_provider", lambda email: calls.append(email) or "unblocked")
    db = FakeDb()
    db.store[("tenants", "t1", "users", "u1")] = {"email": "sam@acme.example"}
    assert worker.handle_event("m-3", {"type": "alerts.resubscribe", "tenantId": "t1", "uid": "u1"}, db=db) == "unblocked"
    assert worker.handle_event("m-4", {"type": "alerts.resubscribe", "tenantId": "t1", "uid": "nobody"}, db=db) == "skipped"
    assert calls == ["sam@acme.example"]


def test_worker_endpoint_status_codes(monkeypatch):
    client = TestClient(worker.app)
    assert client.get("/health").json() == {"status": "ok"}
    # Malformed: acknowledged (204) so Pub/Sub doesn't retry it forever.
    assert client.post("/pubsub/alerts", json={"nope": 1}).status_code == 204
    # Handled: 204.
    monkeypatch.setattr(worker, "handle_event", lambda mid, ev: "sent_added")
    ok = push_body({"type": "regulation.added", "tenantId": "t1", "regulationId": "r1"})
    assert client.post("/pubsub/alerts", json=ok).status_code == 204

    # Unexpected error: 500, so Pub/Sub retries.
    def boom(mid, ev):
        raise RuntimeError("firestore down")
    monkeypatch.setattr(worker, "handle_event", boom)
    assert client.post("/pubsub/alerts", json=ok).status_code == 500
