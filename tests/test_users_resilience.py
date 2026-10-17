"""
T022 - Adding a user survives a dropped connection (M2 QC).

On Oct 16 2026 a dropped connection to Firebase in the middle of adding a
user returned a bare 500 and could leave a login with no role. core/users.py
now retries each call once, removes a half-made login, and answers 503 with
"please try again".

These tests replace the Firebase / Firestore calls with fakes, so they need
no server, make no network calls and create no real users.
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

import time
import types
import pytest
from fastapi import HTTPException
from firebase_admin import auth, exceptions as firebase_exceptions

import core.users as users

TENANT = "test-tenant-001"


def dropped():
    return firebase_exceptions.UnavailableError("connection reset", cause=ConnectionResetError())


def already_exists():
    return auth.EmailAlreadyExistsError("EMAIL_EXISTS", cause=None, http_response=None)


class FakeDoc:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def get(self):
        return types.SimpleNamespace(exists=self.path in self.store.docs or self.path.count("/") == 1)

    def set(self, data):
        self.store.set_calls += 1
        if self.store.fail_set:
            self.store.fail_set -= 1
            raise dropped()
        self.store.docs[self.path] = data

    def collection(self, name):
        return FakeCollection(self.store, f"{self.path}/{name}")


class FakeCollection:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def document(self, doc_id):
        return FakeDoc(self.store, f"{self.path}/{doc_id}")

    def stream(self):
        return [types.SimpleNamespace(id=c) for c in ("BU1", "BU2")]


class FakeDb:
    def __init__(self):
        self.docs, self.set_calls, self.fail_set = {}, 0, 0

    def collection(self, name):
        return FakeCollection(self, name)


class FakeAuth:
    """Records calls; each `fail_*` counter makes that many calls raise first."""

    def __init__(self):
        self.calls = []
        self.fail_create = []      # exceptions to raise, in order
        self.fail_claims = 0
        self.fail_link = 0
        self.claims = {}
        self.deleted = []
        self.existing = None       # what get_user_by_email returns

    def create_user(self, email, password, display_name):
        self.calls.append("create")
        if self.fail_create:
            raise self.fail_create.pop(0)
        return types.SimpleNamespace(uid="new-uid")

    def set_custom_user_claims(self, uid, claims):
        self.calls.append("claims")
        if self.fail_claims:
            self.fail_claims -= 1
            raise dropped()
        self.claims[uid] = claims

    def generate_password_reset_link(self, email):
        self.calls.append("link")
        if self.fail_link:
            self.fail_link -= 1
            raise dropped()
        return "https://example.test/reset"

    def get_user_by_email(self, email):
        if self.existing is None:
            raise auth.UserNotFoundError("not found")
        return self.existing

    def delete_user(self, uid):
        self.deleted.append(uid)


@pytest.fixture
def fakes(monkeypatch):
    fake_auth, fake_db = FakeAuth(), FakeDb()
    for name in ("create_user", "set_custom_user_claims", "generate_password_reset_link",
                 "get_user_by_email", "delete_user"):
        monkeypatch.setattr(users.auth, name, getattr(fake_auth, name))
    monkeypatch.setattr(users, "db", fake_db)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return fake_auth, fake_db


def add(**overrides):
    args = dict(email="x-0000aaaa@testtenant.com", tenant_id=TENANT, role="user", business_unit_ids=["BU1"])
    args.update(overrides)
    return users.create_user_and_get_reset_link(**args)


def mirror(fake_db):
    return fake_db.docs.get(f"tenants/{TENANT}/users/new-uid")


def test_one_dropped_call_is_retried_and_the_user_is_added(fakes):
    fake_auth, fake_db = fakes
    fake_auth.fail_create = [dropped()]
    fake_auth.fail_claims = 1
    uid, link = add()
    assert uid == "new-uid" and link.startswith("https://")
    assert fake_auth.calls.count("create") == 2 and fake_auth.calls.count("claims") == 2
    assert fake_auth.claims["new-uid"]["role"] == "user"
    assert mirror(fake_db)["businessUnitIds"] == ["BU1"]
    assert fake_auth.deleted == []


def test_retry_picks_up_the_login_the_dropped_attempt_made(fakes):
    fake_auth, fake_db = fakes
    fake_auth.fail_create = [dropped(), already_exists()]
    fake_auth.existing = types.SimpleNamespace(
        uid="new-uid", custom_claims=None,
        user_metadata=types.SimpleNamespace(creation_timestamp=time.time() * 1000 - 5000),
    )
    uid, _ = add()
    assert uid == "new-uid"
    assert fake_auth.claims["new-uid"]["tenant_id"] == TENANT
    assert mirror(fake_db) is not None


def test_retry_never_takes_over_a_real_existing_user(fakes):
    fake_auth, fake_db = fakes
    fake_auth.fail_create = [dropped(), already_exists()]
    fake_auth.existing = types.SimpleNamespace(
        uid="someone-else", custom_claims={"tenant_id": TENANT, "role": "admin"},
        user_metadata=types.SimpleNamespace(creation_timestamp=time.time() * 1000 - 5000),
    )
    with pytest.raises(auth.EmailAlreadyExistsError):
        add()
    assert "someone-else" not in fake_auth.claims and fake_auth.deleted == []


def test_duplicate_email_on_first_try_is_not_retried(fakes):
    fake_auth, _ = fakes
    fake_auth.fail_create = [already_exists()]
    with pytest.raises(auth.EmailAlreadyExistsError):
        add()
    assert fake_auth.calls == ["create"]


def test_still_failing_after_retry_removes_the_login_and_says_try_again(fakes):
    fake_auth, fake_db = fakes
    fake_auth.fail_claims = 2
    with pytest.raises(HTTPException) as err:
        add()
    assert err.value.status_code == 503
    assert "try again" in err.value.detail and "Nothing was saved" in err.value.detail
    assert fake_auth.deleted == ["new-uid"]
    assert mirror(fake_db) is None


def test_failed_directory_record_removes_the_login(fakes):
    fake_auth, fake_db = fakes
    fake_db.fail_set = 2
    with pytest.raises(HTTPException) as err:
        add()
    assert err.value.status_code == 503
    assert fake_auth.deleted == ["new-uid"]
    assert mirror(fake_db) is None


def test_failed_password_link_leaves_no_directory_record(fakes):
    fake_auth, fake_db = fakes
    fake_auth.fail_link = 2
    with pytest.raises(HTTPException):
        add()
    assert fake_auth.deleted == ["new-uid"]
    assert fake_db.set_calls == 0  # link comes before the record


def test_create_login_failing_twice_says_try_again_and_saves_nothing(fakes):
    fake_auth, fake_db = fakes
    fake_auth.fail_create = [dropped(), dropped()]
    with pytest.raises(HTTPException) as err:
        add()
    assert err.value.status_code == 503
    assert fake_auth.calls == ["create", "create"] and fake_db.set_calls == 0


def test_non_network_error_is_not_retried_but_still_cleaned_up(fakes, monkeypatch):
    fake_auth, _ = fakes

    def bad_claims(uid, claims):
        fake_auth.calls.append("claims")
        raise ValueError("claims too large")

    monkeypatch.setattr(users.auth, "set_custom_user_claims", bad_claims)
    with pytest.raises(ValueError):
        add()
    assert fake_auth.calls.count("claims") == 1
    assert fake_auth.deleted == ["new-uid"]
