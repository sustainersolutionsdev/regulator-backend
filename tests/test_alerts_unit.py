"""
T025 - email alert logic (core/alerts.py), without a server, Firestore or
Brevo: the database and the email API are replaced by small fakes, so these
tests never send an email or write a record.

Covers: who is emailed (DL-9 opt-out, removed users), the email content
(template v1.1, escaping, links), the Brevo calls (one per person, retry,
refusals, test domains), and the end-to-end alert with its Alerts Log entry,
including that a failure never raises.
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from datetime import datetime, timezone

import pytest
import requests

import core.alerts as alerts
from core.alerts import (
    alert_settings,
    deliver,
    format_due_date,
    render_alert,
    select_recipients,
    send_regulation_added_alert,
    visible_document_count,
)

WHEN = datetime(2026, 10, 20, 9, 5, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(alerts.time, "sleep", lambda s: None)


def cfg(mode="send", **over):
    base = {
        "mode": mode,
        "api_key": "test-key",
        "sender_email": "sender@example.com",
        "sender_name": "Regulator Alerts",
        "app_base_url": "http://localhost:5173",
        "blocked_domains": {"testtenant.com", "tenantb.com"},
    }
    base.update(over)
    return base


# --- Fakes -----------------------------------------------------------------------

class FakeResp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class FakePost:
    """Records every call; answers from a queue (default 201)."""
    def __init__(self, answers=None):
        self.calls, self.answers = [], list(answers or [])

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        answer = self.answers.pop(0) if self.answers else FakeResp(201, {"messageId": f"<m{len(self.calls)}>"})
        if isinstance(answer, Exception):
            raise answer
        return answer


class Snap:
    def __init__(self, doc_id, data):
        self.id, self._data = doc_id, data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeDoc:
    def __init__(self, store, path):
        self.store, self.path = store, path
        self.id = path.rsplit("/", 1)[-1]

    def get(self):
        return Snap(self.id, self.store.get(self.path))

    def set(self, data, merge=False):
        self.store[self.path] = dict(data)

    def collection(self, name):
        return FakeCol(self.store, f"{self.path}/{name}")


class FakeCol:
    counter = 0

    def __init__(self, store, path):
        self.store, self.path = store, path

    def document(self, doc_id=None):
        if doc_id is None:
            FakeCol.counter += 1
            doc_id = f"auto{FakeCol.counter}"
        return FakeDoc(self.store, f"{self.path}/{doc_id}")

    def stream(self):
        prefix = self.path + "/"
        for path, data in list(self.store.items()):
            rest = path[len(prefix):] if path.startswith(prefix) else None
            if rest and "/" not in rest:
                yield Snap(rest, data)


class FakeDb:
    def __init__(self, store):
        self.store = store

    def collection(self, name):
        return FakeCol(self.store, name)


def tenant_store(reg_overrides=None, prefs=None):
    t = "tenants/t1"
    reg = {
        "title": "Regulation (EU) 2023/1542 on batteries - digital battery passport",
        "abbreviatedTitle": "EU Battery Passport",
        "dueDate": "2027-02-18",
        "domain": "Batteries",
        "country": "European Union",
        "region": "Europe",
        "businessUnitIds": ["BU1", "BU2"],
        "notes": "Applies to EV and industrial batteries over 2 kWh.",
        "documents": [],
        "addToRoadmapDashboard": True,
        "hidden": False,
    }
    reg.update(reg_overrides or {})
    store = {
        t: {"name": "Acme Devices"},
        f"{t}/businessUnits/BU1": {"code": "BU1", "label": "Default"},
        f"{t}/businessUnits/BU2": {"code": "BU2", "label": "Printers"},
        f"{t}/users/u-admin": {"email": "priya@acme.example", "displayName": "Priya Shah", "role": "sme"},
        f"{t}/users/u-user": {"email": "sam@acme.example", "displayName": "Sam Lee", "role": "user"},
        f"{t}/users/u-gone": {"email": "gone@acme.example", "displayName": "Left", "role": "user", "hidden": True},
        f"{t}/users/u-test": {"email": "user@testtenant.com", "displayName": "Test", "role": "user"},
        f"{t}/regulations/r1": reg,
        "tenants/t2/users/u-other": {"email": "other@tenantb.example", "displayName": "Other tenant", "role": "admin"},
    }
    for uid, value in (prefs or {}).items():
        store[f"{t}/userPreferences/{uid}"] = value
    return store


def alert_logs(store, tenant="t1"):
    prefix = f"tenants/{tenant}/alertsLog/"
    return [v for k, v in store.items() if k.startswith(prefix)]


# --- Settings ----------------------------------------------------------------------

def test_mode_defaults_to_log_and_rejects_unknown(monkeypatch):
    monkeypatch.delenv("ALERTS_MODE", raising=False)
    assert alert_settings()["mode"] == "log"
    monkeypatch.setenv("ALERTS_MODE", "SEND")
    assert alert_settings()["mode"] == "send"
    monkeypatch.setenv("ALERTS_MODE", "everyone")
    assert alert_settings()["mode"] == "log"


# --- Who is emailed ----------------------------------------------------------------

def test_recipients_skip_removed_opted_out_and_blank_emails():
    users = [
        {"id": "a", "email": "a@x.example", "displayName": "A"},
        {"id": "b", "email": "b@x.example"},
        {"id": "c", "email": "c@x.example", "hidden": True},
        {"id": "d", "email": ""},
        {"id": "e", "email": "A@X.example"},  # same address as a
    ]
    recipients, opted_out, no_email, removed = select_recipients(users, {"b": {"emailAlerts": False}})
    assert [r["uid"] for r in recipients] == ["a"]
    assert opted_out == ["b"] and no_email == ["d"] and removed == ["c"]


def test_alerts_are_on_when_no_preference_saved_or_turned_back_on():
    users = [{"id": "a", "email": "a@x.example"}, {"id": "b", "email": "b@x.example"}]
    recipients, opted_out, _, _ = select_recipients(users, {"b": {"emailAlerts": True, "dashboardView": "view=bu"}})
    assert [r["uid"] for r in recipients] == ["a", "b"] and opted_out == []


# --- Content ------------------------------------------------------------------------

def render(reg=None, **kw):
    base = {"id": "r1", "title": "Full Title", "abbreviatedTitle": "Short", "dueDate": "2027-02-18",
            "businessUnitIds": ["BU1"], "domain": "Batteries"}
    base.update(reg or {})
    return render_alert("added", base, tenant_name="Acme", bu_labels={"BU1": "Default"},
                        actor={"displayName": "Priya Shah", "role": "sme"},
                        app_base_url="http://localhost:5173", when=WHEN, **kw)


def test_subject_and_dates():
    msg = render()
    assert msg["subject"] == "[Regulator] New regulation: Short (due 18 Feb 2027)"
    assert format_due_date("2027-02-18") == "18 Feb 2027"
    assert format_due_date("not a date") == "not a date"
    assert "20 Oct 2026, 09:05 UTC" in msg["html"]


def test_subject_is_one_line_and_falls_back_to_title():
    msg = render({"abbreviatedTitle": "", "title": "Line one\r\nBcc: x@evil.example"})
    assert "\n" not in msg["subject"] and "\r" not in msg["subject"]
    assert msg["subject"].startswith("[Regulator] New regulation: Line one Bcc:")


def test_record_values_are_escaped():
    msg = render({"title": '<script>alert("x")</script>', "notes": "<b>bold</b>"})
    assert "<script>" not in msg["html"] and "&lt;script&gt;" in msg["html"]
    assert "<b>bold</b>" not in msg["html"]


def test_template_sections_links_and_not_set_status():
    msg = render({"businessUnitIds": ["BU1", "BU9"]})
    html = msg["html"]
    for text in ("New regulation added", "Details", "Status by Business Unit",
                 "Set by people on the Dashboard, never by AI.", "Default (BU1)", "BU9",
                 "Added by Priya Shah (SME)", "Turn off email alerts", "with email alerts on"):
        assert text in html, text
    assert html.count("Not set") == 2
    assert 'href="http://localhost:5173/dashboard?reg=r1"' in html
    assert 'href="http://localhost:5173/account/email-alerts"' in html
    assert "AI summary" not in html  # M4, not in Milestone 3 emails (DL-9)
    assert "View in Regulator: http://localhost:5173/dashboard?reg=r1" in msg["text"]
    assert "Turn off email alerts: http://localhost:5173/account/email-alerts" in msg["text"]


def test_set_status_and_explanation_are_shown():
    msg = render({"buStatuses": {"BU1": {"status": "action_needed", "note": "No owner yet"}}})
    assert "Action needed" in msg["html"] and "No owner yet" in msg["html"]
    assert "- Default (BU1): Action needed - No owner yet" in msg["text"]


def test_hidden_documents_are_not_counted():
    reg = {"documents": [{"gcsUri": "gs://b/a"}, {"gcsUri": "gs://b/b"}],
           "hiddenDocuments": {alerts._document_key("gs://b/b"): {}}}
    assert visible_document_count(reg) == 1


# --- Sending -----------------------------------------------------------------------

PEOPLE = [{"uid": "a", "email": "a@x.example", "name": "A"}, {"uid": "b", "email": "b@x.example", "name": "B"}]
MSG = {"subject": "S", "html": "<p>H</p>", "text": "T"}


def test_log_mode_sends_nothing():
    post = FakePost()
    out = deliver(cfg("log"), PEOPLE, MSG, post=post)
    assert out["status"] == "logged" and post.calls == []


def test_one_call_per_person_with_only_their_address():
    post = FakePost()
    out = deliver(cfg(), PEOPLE, MSG, post=post)
    assert out["status"] == "sent" and out["sent"] == ["a", "b"]
    assert [c["json"]["to"] for c in post.calls] == [[{"email": "a@x.example", "name": "A"}],
                                                     [{"email": "b@x.example", "name": "B"}]]
    first = post.calls[0]
    assert first["url"] == alerts.BREVO_URL and first["headers"]["api-key"] == "test-key"
    assert first["json"]["sender"] == {"name": "Regulator Alerts", "email": "sender@example.com"}
    assert first["json"]["textContent"] == "T"


def test_test_domains_are_never_emailed():
    post = FakePost()
    out = deliver(cfg(), [{"uid": "t", "email": "user@TestTenant.com", "name": "T"}], MSG, post=post)
    assert post.calls == [] and out["skipped_test_domain"] == ["t"] and out["status"] == "no_recipients"


def test_retry_once_on_server_error_or_dropped_connection():
    post = FakePost([FakeResp(503), requests.exceptions.ConnectionError(), FakeResp(201, {"messageId": "<ok>"})])
    out = deliver(cfg(), PEOPLE, MSG, post=post)
    # a: 503 then dropped -> failed after its one retry; b: sent first time
    assert out["failed"] == [{"uid": "a", "error": "ConnectionError"}]
    assert out["sent"] == ["b"] and out["status"] == "partly_sent" and len(post.calls) == 3


def test_refusal_is_not_retried():
    post = FakePost([FakeResp(401, {"message": "Key not found"}), FakeResp(401, {"message": "Key not found"})])
    out = deliver(cfg(), PEOPLE, MSG, post=post)
    assert out["status"] == "failed" and len(post.calls) == 2  # one per person, no retries
    assert out["failed"][0]["error"] == "HTTP 401: Key not found"
    assert "test-key" not in str(out)


def test_missing_key_fails_without_calling_brevo():
    post = FakePost()
    out = deliver(cfg(api_key=""), PEOPLE, MSG, post=post)
    assert out["status"] == "failed" and post.calls == []


# --- End to end (fake database) ----------------------------------------------------

def run_alert(monkeypatch, store, mode="send", post=None):
    monkeypatch.setenv("ALERTS_MODE", mode)
    monkeypatch.setenv("BREVO_API_KEY", "test-key")
    monkeypatch.setenv("ALERT_SENDER_EMAIL", "sender@example.com")
    post = post or FakePost()
    entry = send_regulation_added_alert("t1", "r1", "u-admin", db=FakeDb(store), post=post)
    return entry, post


def test_added_alert_emails_tenant_users_and_writes_the_log(monkeypatch):
    store = tenant_store(prefs={"u-user": {"emailAlerts": True}})
    entry, post = run_alert(monkeypatch, store)
    sent_to = sorted(c["json"]["to"][0]["email"] for c in post.calls)
    assert sent_to == ["priya@acme.example", "sam@acme.example"]  # not removed, not other tenant, not test domain
    assert entry["status"] == "sent" and entry["sentCount"] == 2
    assert entry["skippedTestDomain"] == ["u-test"]
    assert entry["skippedRemoved"] == ["u-gone"]
    assert "Added by Priya Shah (SME)" in post.calls[0]["json"]["htmlContent"]
    logs = alert_logs(store)
    assert len(logs) == 1 and logs[0]["regulationId"] == "r1" and logs[0]["type"] == "regulation_added"
    assert "priya@acme.example" not in str(logs[0])  # user IDs only, never addresses


def test_opted_out_user_is_not_emailed(monkeypatch):
    store = tenant_store(prefs={"u-user": {"emailAlerts": False}})
    entry, post = run_alert(monkeypatch, store)
    assert [c["json"]["to"][0]["email"] for c in post.calls] == ["priya@acme.example"]
    assert entry["skippedOptedOut"] == ["u-user"]


@pytest.mark.parametrize("overrides", [{"addToRoadmapDashboard": False}, {"hidden": True}])
def test_no_alert_when_not_on_dashboard_or_removed(monkeypatch, overrides):
    store = tenant_store(overrides)
    entry, post = run_alert(monkeypatch, store)
    assert entry is None and post.calls == [] and alert_logs(store) == []


def test_off_mode_does_nothing(monkeypatch):
    store = tenant_store()
    entry, post = run_alert(monkeypatch, store, mode="off")
    assert entry is None and post.calls == [] and alert_logs(store) == []


def test_log_mode_writes_the_log_but_sends_nothing(monkeypatch):
    store = tenant_store()
    entry, post = run_alert(monkeypatch, store, mode="log")
    assert post.calls == [] and entry["status"] == "logged" and entry["recipientCount"] == 3


def test_unexpected_error_is_recorded_not_raised(monkeypatch):
    store = tenant_store()

    def boom(*a, **k):
        raise RuntimeError("template bug")

    monkeypatch.setattr(alerts, "render_alert", boom)
    entry, post = run_alert(monkeypatch, store)
    assert entry is None and post.calls == []
    logs = alert_logs(store)
    assert len(logs) == 1 and logs[0]["status"] == "failed" and logs[0]["error"] == "RuntimeError"
