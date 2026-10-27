"""
T061 - invitation email for new users (OPEN-16 part 1, approved Oct 26).
No server, Firestore, Firebase or Brevo needed: fakes throughout, like
test_alerts_unit.py.
"""
import base64
import json

import pytest

from core import alerts, invitations


# --- fakes -------------------------------------------------------------------

class Snap:
    def __init__(self, doc_id, data):
        self.id, self._data = doc_id, data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class Doc:
    def __init__(self, store, path):
        self.store, self.path = store, path
        self.id = path.rsplit("/", 1)[-1]

    def get(self):
        return Snap(self.id, self.store.get(self.path))

    def set(self, data):
        self.store[self.path] = data

    def collection(self, name):
        return Col(self.store, f"{self.path}/{name}")


class Col:
    counter = 0

    def __init__(self, store, path):
        self.store, self.path = store, path

    def document(self, doc_id=None):
        if doc_id is None:
            Col.counter += 1
            doc_id = f"auto{Col.counter}"
        return Doc(self.store, f"{self.path}/{doc_id}")

    def stream(self):
        prefix = self.path + "/"
        return [Snap(k[len(prefix):], v) for k, v in self.store.items()
                if k.startswith(prefix) and "/" not in k[len(prefix):]]


class FakeDb:
    def __init__(self, store):
        self.store = store

    def collection(self, name):
        return Col(self.store, name)


class Resp:
    status_code = 201

    def json(self):
        return {"messageId": "m-1"}


def make_store(person=None):
    t = "tenants/t1"
    return {
        t: {"name": "Acme Corp"},
        f"{t}/businessUnits/BU1": {"code": "BU1", "label": "Printers"},
        f"{t}/businessUnits/BU2": {"code": "BU2", "label": "Data Centers"},
        f"{t}/users/admin1": {"email": "boss@acme.example", "displayName": "Pat Admin", "role": "admin"},
        f"{t}/users/u1": person if person is not None else {
            "email": "sam@acme.example", "displayName": "Sam <Lee>", "role": "user", "businessUnitIds": ["BU1", "BU2"],
        },
    }


@pytest.fixture
def send_mode(monkeypatch):
    monkeypatch.setenv("ALERTS_MODE", "send")
    monkeypatch.setenv("BREVO_API_KEY", "key")
    monkeypatch.setenv("ALERT_SENDER_EMAIL", "noreply@example.com")
    monkeypatch.setenv("APP_BASE_URL", "https://regulator-dev.web.app")
    monkeypatch.setenv("ALERTS_BLOCKED_DOMAINS", "testtenant.com")


def log_entries(store):
    return [v for k, v in store.items() if "/alertsLog/" in k]


# --- wording -----------------------------------------------------------------

def test_render_uses_the_approved_wording_and_escapes():
    msg = invitations.render_invitation(
        {"email": "sam@acme.example", "displayName": "Sam <Lee>", "role": "user", "businessUnitIds": ["BU1", "BU2"]},
        tenant_name="Acme Corp", bu_labels={"BU1": "Printers", "BU2": "Data Centers"},
        actor={"displayName": "Pat Admin"}, link="https://x.example/reset?oobCode=abc&mode=resetPassword",
        app_base_url="https://regulator-dev.web.app",
    )
    assert msg["subject"] == "You've been added to Regulator — Acme Corp"
    text = msg["text"]
    assert "Hello Sam <Lee>," in text
    assert "Pat Admin has added you to Regulator for Acme Corp as User, for Printers and Data Centers." in text
    assert "This button works for 1 hour." in text and '"Forgot password?"' in text
    assert "no one can sign in as you without setting a password from your inbox" in text
    html = msg["html"]
    assert "Sam &lt;Lee&gt;" in html and "<Lee>" not in html
    assert "Set my password" in html and "oobCode=abc&amp;mode=resetPassword" in html


def test_admin_and_sme_cover_all_business_units():
    for role, label in (("admin", "Admin"), ("sme", "SME")):
        msg = invitations.render_invitation(
            {"email": "a@acme.example", "displayName": "A", "role": role}, tenant_name="Acme",
            bu_labels={}, actor={}, link="L", app_base_url="U")
        assert f"as {label}, for all Business Units." in msg["text"]
        assert "An administrator has added you" in msg["text"]


# --- sending -------------------------------------------------------------------

def test_send_mode_emails_the_person_with_a_fresh_link(send_mode):
    store, posts, links = make_store(), [], []
    post = lambda url, **kw: posts.append(kw["json"]) or Resp()
    make_link = lambda email, base: links.append((email, base)) or "https://auth.example/reset?oobCode=SECRET"
    entry = invitations.send_invitation("t1", "u1", "admin1", db=FakeDb(store), post=post, make_link=make_link)
    assert links == [("sam@acme.example", "https://regulator-dev.web.app")]
    assert len(posts) == 1 and posts[0]["to"] == [{"email": "sam@acme.example", "name": "Sam <Lee>"}]
    assert "oobCode=SECRET" in posts[0]["htmlContent"]
    assert entry["status"] == "sent" and entry["type"] == "user_invited" and entry["userUid"] == "u1"
    # The Alerts Log never holds the link or the address.
    stored = json.dumps(log_entries(store), default=str)
    assert "SECRET" not in stored and "sam@acme.example" not in stored


def test_resend_is_logged_as_resent(send_mode):
    store = make_store()
    entry = invitations.send_invitation("t1", "u1", "admin1", resend=True, db=FakeDb(store),
                                        post=lambda url, **kw: Resp(), make_link=lambda e, b: "L")
    assert entry["type"] == "invitation_resent"


def test_sent_even_when_email_alerts_are_off(send_mode):
    store = make_store()
    store["tenants/t1/userPreferences/u1"] = {"emailAlerts": False}
    posts = []
    invitations.send_invitation("t1", "u1", "admin1", db=FakeDb(store),
                                post=lambda url, **kw: posts.append(1) or Resp(), make_link=lambda e, b: "L")
    assert posts == [1]


def test_log_mode_makes_no_link_and_sends_nothing(monkeypatch):
    monkeypatch.setenv("ALERTS_MODE", "log")
    store = make_store()
    called = []
    entry = invitations.send_invitation("t1", "u1", "admin1", db=FakeDb(store),
                                        post=lambda *a, **k: called.append("post"),
                                        make_link=lambda *a: called.append("link"))
    assert called == [] and entry["status"] == "logged"


def test_test_domain_is_never_emailed(send_mode):
    store = make_store({"email": "x@testtenant.com", "displayName": "T", "role": "user", "businessUnitIds": ["BU1"]})
    called = []
    entry = invitations.send_invitation("t1", "u1", "admin1", db=FakeDb(store),
                                        post=lambda *a, **k: called.append("post"),
                                        make_link=lambda *a: called.append("link"))
    assert called == [] and entry["skippedTestDomain"] == ["u1"]


def test_removed_or_unknown_user_gets_nothing(send_mode):
    store = make_store({"email": "sam@acme.example", "role": "user", "hidden": True})
    assert invitations.send_invitation("t1", "u1", "admin1", db=FakeDb(store), make_link=lambda *a: "L") is None
    assert invitations.send_invitation("t1", "nobody", "admin1", db=FakeDb(make_store()), make_link=lambda *a: "L") is None


def test_link_failure_is_logged_never_raised(send_mode):
    store = make_store()

    def boom(*a):
        raise RuntimeError("firebase down")

    assert invitations.send_invitation("t1", "u1", "admin1", db=FakeDb(store), make_link=boom) is None
    entries = log_entries(store)
    assert entries and entries[-1]["status"] == "failed" and entries[-1]["error"] == "RuntimeError"


def test_off_mode_does_nothing(monkeypatch):
    monkeypatch.setenv("ALERTS_MODE", "off")
    store = make_store()
    assert invitations.send_invitation("t1", "u1", "admin1", db=FakeDb(store)) is None
    assert log_entries(store) == []


# --- hand-off --------------------------------------------------------------------

class FakeTasks:
    def __init__(self):
        self.calls = []

    def add_task(self, fn, *args):
        self.calls.append((fn, args))


def test_inline_mode_runs_after_the_response(monkeypatch):
    monkeypatch.setenv("ALERTS_DISPATCH", "inline")
    tasks = FakeTasks()
    invitations.schedule_invitation(tasks, "t1", "u1", "admin1")
    assert tasks.calls == [(invitations.send_invitation, ("t1", "u1", "admin1", False))]


def test_pubsub_event_carries_ids_only(monkeypatch):
    monkeypatch.setenv("ALERTS_DISPATCH", "pubsub")
    published = []
    from core import dispatch
    monkeypatch.setattr(dispatch, "publish_event", lambda event: published.append(event) or "msg-1")
    tasks = FakeTasks()
    invitations.schedule_invitation(tasks, "t1", "u1", "admin1", resend=True)
    assert tasks.calls == []
    assert published == [{"type": "user.invited", "tenantId": "t1", "uid": "u1", "actorUid": "admin1", "resend": True}]


def test_worker_handles_user_invited_once(monkeypatch):
    import worker
    calls = []
    monkeypatch.setattr(invitations, "send_invitation", lambda *a, **k: calls.append((a, k)))
    seen = set()

    def claim(db, tenant_id, message_id, event_type):
        if message_id in seen:
            return False
        seen.add(message_id)
        return True

    monkeypatch.setattr(worker, "claim_message", claim)
    event = {"type": "user.invited", "tenantId": "t1", "uid": "u1", "actorUid": "admin1", "resend": False}
    envelope = {"message": {"messageId": "m1", "data": base64.b64encode(json.dumps(event).encode()).decode()}}
    mid, decoded = worker.decode_push(envelope)
    assert worker.handle_event(mid, decoded, db=object()) == "sent_invitation"
    assert worker.handle_event(mid, decoded, db=object()) == "duplicate"
    assert len(calls) == 1 and calls[0][0] == ("t1", "u1", "admin1")
