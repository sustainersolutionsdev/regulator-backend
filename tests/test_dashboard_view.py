"""
T020 - Saved Dashboard view (DL-8): the Dashboard opens as each person last
left it, on any computer. GET/PUT /me/dashboard-view, scoped to the caller.

The fixed test accounts' saved views are overwritten by each run; they are
preferences, not test records, so cleanup leaves them.
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

import uuid
import httpx
from core.get_test_token import get_id_token

BASE_URL = "http://127.0.0.1:8000"
TIMEOUT = 30.0

TEST_PASSWORD = os.environ["TEST_ACCOUNTS_PASSWORD"].strip()
TENANT_A_ADMIN = ("admin@testtenant.com", TEST_PASSWORD)
TENANT_A_USER = ("user@testtenant.com", TEST_PASSWORD)


def auth_header(email, password):
    return {"Authorization": f"Bearer {get_id_token(email, password)}"}


def put_view(view, headers):
    return httpx.put(f"{BASE_URL}/me/dashboard-view", json={"view": view}, headers=headers, timeout=TIMEOUT)


def get_view(headers):
    return httpx.get(f"{BASE_URL}/me/dashboard-view", headers=headers, timeout=TIMEOUT)


def test_dashboard_view_requires_token():
    assert httpx.get(f"{BASE_URL}/me/dashboard-view", timeout=TIMEOUT).status_code == 401
    assert httpx.put(f"{BASE_URL}/me/dashboard-view", json={"view": ""}, timeout=TIMEOUT).status_code == 401


def test_saved_view_round_trips_and_strips_question_mark():
    admin = auth_header(*TENANT_A_ADMIN)
    marker = uuid.uuid4().hex[:8]
    view = f"?view=bu&status=action_needed&q={marker}"
    assert put_view(view, admin).status_code == 200
    assert get_view(admin).json()["view"] == view.lstrip("?")


def test_each_person_has_their_own_view():
    admin, user = auth_header(*TENANT_A_ADMIN), auth_header(*TENANT_A_USER)
    put_view("view=regulation&sort=name", admin)
    put_view("view=bu&bu=BU1", user)
    assert get_view(admin).json()["view"] == "view=regulation&sort=name"
    assert get_view(user).json()["view"] == "view=bu&bu=BU1"


def test_too_long_or_multiline_view_rejected():
    admin = auth_header(*TENANT_A_ADMIN)
    assert put_view("q=" + "x" * 2000, admin).status_code == 400
    assert put_view("view=bu\nq=x", admin).status_code == 400


def test_empty_view_clears_it():
    admin = auth_header(*TENANT_A_ADMIN)
    put_view("view=bu", admin)
    assert put_view("", admin).status_code == 200
    assert get_view(admin).json()["view"] == ""
