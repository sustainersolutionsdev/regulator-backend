"""
T028: Gemini client, usage log, daily limit and the AI status/usage routes.
No server, Firestore or network needed - fakes throughout, like
test_alerts_unit.py and test_deploy_dispatch.py. Never calls Gemini.
"""
import datetime as dt

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import ai_client
from core.ai_client import (AICreditsExhausted, AIError, AILimitReached, AINoAnswer,
                            AINotConfigured, generate, generate_json)

NOW = dt.datetime(2026, 11, 10, 9, 30, tzinfo=dt.timezone.utc)
DAY = "2026-11-10"


# --- fakes --------------------------------------------------------------------

class Inc:
    def __init__(self, v):
        self.v = v


class FakeSnap:
    def __init__(self, doc_id, data):
        self.id, self._data = doc_id, data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeDoc:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def collection(self, name):
        return FakeCol(self.db, self.path + (name,))

    def get(self):
        return FakeSnap(self.path[-1], self.db.docs.get(self.path))

    def set(self, data, merge=False):
        cur = dict(self.db.docs.get(self.path) or {}) if merge else {}
        for k, v in data.items():
            cur[k] = (cur.get(k, 0) + v.v) if isinstance(v, Inc) else v
        self.db.docs[self.path] = cur


class FakeCol:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def document(self, name):
        return FakeDoc(self.db, self.path + (name,))

    def add(self, data):
        if self.db.fail_add:
            raise RuntimeError("firestore down")
        self.db.added.setdefault(self.path, []).append(data)
        return (None, None)

    def stream(self):
        n = len(self.path)
        return [FakeSnap(p[-1], d) for p, d in self.db.docs.items() if p[:n] == self.path and len(p) == n + 1]


class FakeDb:
    def __init__(self):
        self.docs, self.added, self.fail_add = {}, {}, False

    def collection(self, name):
        return FakeCol(self, (name,))

    def entries(self, tenant="t1"):
        return self.added.get(("tenants", tenant, "aiUsage"), [])

    def daily(self, tenant="t1"):
        return self.docs.get(("tenants", tenant, "aiUsageDaily", DAY))


class Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakePost:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ok(text="A plain-language summary.", prompt=1000, out=120, thoughts=0, parts=None):
    usage = {"promptTokenCount": prompt, "candidatesTokenCount": out, "totalTokenCount": prompt + out + thoughts}
    if thoughts:
        usage["thoughtsTokenCount"] = thoughts
    return Resp(200, {
        "candidates": [{"content": {"parts": parts or [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": usage, "modelVersion": "gemini-3.1-flash-lite",
    })


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    for name in ("AI_DAILY_CALL_LIMIT", "GEMINI_PRICE_INPUT_PER_M", "GEMINI_PRICE_OUTPUT_PER_M",
                 "GEMINI_THINKING_LEVEL", "GEMINI_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ai_client, "_increment", Inc)
    monkeypatch.setattr(ai_client, "_sleep", lambda s: None)


def call(db, post, **kw):
    return generate("t1", "regulation_summary", kw.pop("contents", "Summarise this."),
                    db=db, post=post, now=NOW, actor_uid="u1", **kw)


# --- configuration ------------------------------------------------------------

def test_not_configured_raises_without_calling(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    post, db = FakePost(), FakeDb()
    with pytest.raises(AINotConfigured):
        call(db, post)
    assert post.calls == [] and db.entries() == []
    assert ai_client.is_enabled() is False


def test_defaults():
    s = ai_client.settings()
    assert s["model"] == "gemini-3.1-flash-lite" and s["daily_limit"] == 300
    assert (s["price_in"], s["price_out"]) == (0.25, 1.50)


def test_bad_numeric_settings_fall_back(monkeypatch):
    monkeypatch.setenv("AI_DAILY_CALL_LIMIT", "lots")
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "")
    s = ai_client.settings()
    assert s["daily_limit"] == 300 and s["timeout"] == 45.0


# --- a normal call ------------------------------------------------------------

def test_success_builds_request_and_logs_usage():
    post, db = FakePost(ok(prompt=2000, out=400)), FakeDb()
    r = call(db, post, system="You write regulation summaries.", max_output_tokens=600)

    req = post.calls[0]
    assert req["url"].endswith("/models/gemini-3.1-flash-lite:generateContent")
    assert req["headers"]["x-goog-api-key"] == "test-key"
    body = req["json"]
    assert body["contents"] == [{"role": "user", "parts": [{"text": "Summarise this."}]}]
    assert body["systemInstruction"] == {"parts": [{"text": "You write regulation summaries."}]}
    assert body["generationConfig"] == {"maxOutputTokens": 600, "temperature": 0.2}

    assert r.text == "A plain-language summary." and r.model == "gemini-3.1-flash-lite"
    assert (r.input_tokens, r.output_tokens) == (2000, 400)
    assert r.cost_usd == pytest.approx(2000 * 0.25 / 1e6 + 400 * 1.5 / 1e6)

    [entry] = db.entries()
    assert entry["feature"] == "regulation_summary" and entry["status"] == "ok" and entry["uid"] == "u1"
    assert entry["inputTokens"] == 2000 and entry["outputTokens"] == 400
    daily = db.daily()
    assert daily["calls"] == 1 and daily["inputTokens"] == 2000 and daily["outputTokens"] == 400
    assert daily["costUsd"] == pytest.approx(r.cost_usd) and daily["date"] == DAY


def test_usage_log_never_holds_prompt_or_answer():
    post, db = FakePost(ok(text="SECRET ANSWER")), FakeDb()
    call(db, post, contents="SECRET PROMPT about a red unit")
    blob = repr(db.entries()) + repr(db.daily())
    assert "SECRET" not in blob and "test-key" not in blob


def test_thinking_parts_are_hidden_and_billed_as_output():
    parts = [{"text": "internal reasoning", "thought": True}, {"text": "Final answer."}]
    post, db = FakePost(ok(parts=parts, prompt=100, out=50, thoughts=300)), FakeDb()
    r = call(db, post)
    assert r.text == "Final answer."
    assert r.output_tokens == 350 and db.entries()[0]["thoughtsTokens"] == 300


def test_documents_and_json_mode_and_thinking_level(monkeypatch):
    monkeypatch.setenv("GEMINI_THINKING_LEVEL", "low")
    doc = {"inline_data": {"mime_type": "application/pdf", "data": "JVBERi0x"}}
    post, db = FakePost(ok(text='{"summary": "x"}')), FakeDb()
    data, r = generate_json("t1", "regulation_summary", ["Read this:", doc], db=db, post=post, now=NOW)
    body = post.calls[0]["json"]
    assert body["contents"][0]["parts"] == [{"text": "Read this:"}, doc]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    assert data == {"summary": "x"}


def test_json_in_code_fence_is_accepted():
    post = FakePost(ok(text='```json\n{"a": 1}\n```'))
    data, _ = generate_json("t1", "f", "x", db=FakeDb(), post=post, now=NOW)
    assert data == {"a": 1}


def test_invalid_json_is_no_answer_but_still_counted():
    post, db = FakePost(ok(text="not json")), FakeDb()
    with pytest.raises(AINoAnswer):
        generate_json("t1", "f", "x", db=db, post=post, now=NOW)
    assert db.daily()["calls"] == 1


def test_price_settings_change_the_estimate(monkeypatch):
    monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", "1.50")
    monkeypatch.setenv("GEMINI_PRICE_OUTPUT_PER_M", "7.50")
    r = call(FakeDb(), FakePost(ok(prompt=1_000_000, out=1_000_000)))
    assert r.cost_usd == pytest.approx(9.0)


# --- limits and failures ------------------------------------------------------

def test_daily_limit_blocks_before_calling(monkeypatch):
    monkeypatch.setenv("AI_DAILY_CALL_LIMIT", "2")
    db = FakeDb()
    db.docs[("tenants", "t1", "aiUsageDaily", DAY)] = {"calls": 2}
    post = FakePost()
    with pytest.raises(AILimitReached) as e:
        call(db, post)
    assert e.value.http_status == 429 and post.calls == []
    assert db.entries()[0]["status"] == "limited" and db.daily()["calls"] == 2


def test_limit_is_per_company_and_per_day(monkeypatch):
    monkeypatch.setenv("AI_DAILY_CALL_LIMIT", "1")
    db = FakeDb()
    db.docs[("tenants", "other", "aiUsageDaily", DAY)] = {"calls": 5}
    db.docs[("tenants", "t1", "aiUsageDaily", "2026-11-09")] = {"calls": 5}
    call(db, FakePost(ok()))  # t1 today: 0 used
    with pytest.raises(AILimitReached):
        call(db, FakePost())


@pytest.mark.parametrize("resp", [
    Resp(429, {"error": {"message": "Your prepayment credits are depleted."}}),
    Resp(402, {"error": {"message": "Payment required"}}),
])
def test_credit_problems_are_not_retried(resp):
    post, db = FakePost(resp, ok()), FakeDb()
    with pytest.raises(AICreditsExhausted) as e:
        call(db, post)
    assert len(post.calls) == 1
    assert "administrator" in e.value.message
    assert db.entries()[0]["errorCode"] == "credits_exhausted" and db.daily() is None


def test_dropped_connection_is_retried_once():
    post = FakePost(httpx.ConnectError("reset"), ok())
    assert call(FakeDb(), post).text and len(post.calls) == 2


def test_rate_limit_and_server_errors_retry_once_then_fail():
    post, db = FakePost(Resp(503, {"error": {"message": "overloaded"}}),
                        Resp(429, {"error": {"message": "Resource exhausted (rate)"}})), FakeDb()
    with pytest.raises(AIError) as e:
        call(db, post)
    assert type(e.value) is AIError and len(post.calls) == 2
    assert db.entries()[0]["status"] == "error" and db.daily() is None


def test_bad_key_or_model_is_not_retried():
    post = FakePost(Resp(404, {"error": {"message": "models/x is not found"}}), ok())
    with pytest.raises(AIError):
        call(FakeDb(), post)
    assert len(post.calls) == 1


def test_error_body_without_json_is_handled():
    post = FakePost(Resp(500, None, "<html>bad gateway</html>"), Resp(500, None, "again"))
    with pytest.raises(AIError):
        call(FakeDb(), post)


def test_empty_answer_is_no_answer_but_billed():
    blocked = Resp(200, {"candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}],
                         "usageMetadata": {"promptTokenCount": 800, "candidatesTokenCount": 0}})
    post, db = FakePost(blocked), FakeDb()
    with pytest.raises(AINoAnswer):
        call(db, post)
    assert db.entries()[0]["status"] == "no_answer" and db.entries()[0]["errorCode"] == "SAFETY"
    assert db.daily()["calls"] == 1 and db.daily()["inputTokens"] == 800


def test_usage_log_failure_does_not_lose_the_answer():
    db = FakeDb()
    db.fail_add = True
    assert call(db, FakePost(ok())).text == "A plain-language summary."


# --- routes -------------------------------------------------------------------

from core import ai_routes  # noqa: E402
from core.deps import get_current_context  # noqa: E402


class Ctx:
    def __init__(self, db, role="user", tenant="t1"):
        self.uid, self.role, self.tenant_id, self.business_unit_ids = "u1", role, tenant, ["BU1"]
        self.tenant_ref = db.collection("tenants").document(tenant)


def client_for(db, role):
    app = FastAPI()
    app.include_router(ai_routes.router)
    app.dependency_overrides[get_current_context] = lambda: Ctx(db, role)
    return TestClient(app)


def test_status_for_any_role(monkeypatch):
    monkeypatch.setenv("AI_DAILY_CALL_LIMIT", "10")
    db = FakeDb()
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    db.docs[("tenants", "t1", "aiUsageDaily", today)] = {"calls": 3}
    body = client_for(db, "user").get("/ai/status").json()
    assert body == {"enabled": True, "dailyLimit": 10, "usedToday": 3, "remainingToday": 7}


def test_status_when_ai_is_off(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    assert client_for(FakeDb(), "user").get("/ai/status").json()["enabled"] is False


def test_usage_is_admin_sme_only_and_own_company():
    db = FakeDb()
    today = dt.datetime.now(dt.timezone.utc)
    d0, d1 = today.strftime("%Y-%m-%d"), (today - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    old = (today - dt.timedelta(days=40)).strftime("%Y-%m-%d")
    for d, calls in ((d0, 3), (d1, 2), (old, 9)):
        db.docs[("tenants", "t1", "aiUsageDaily", d)] = {"date": d, "calls": calls, "inputTokens": 100 * calls,
                                                          "outputTokens": 10 * calls, "costUsd": 0.001 * calls}
    db.docs[("tenants", "other", "aiUsageDaily", d0)] = {"date": d0, "calls": 99}

    assert client_for(db, "user").get("/ai/usage").status_code == 403
    body = client_for(db, "sme").get("/ai/usage?days=30").json()
    assert [r["date"] for r in body["days"]] == [d0, d1]
    assert body["totals"]["calls"] == 5 and body["totals"]["inputTokens"] == 500
    assert body["totals"]["costUsd"] == pytest.approx(0.005)
    assert "AI Studio" in body["note"]
    assert client_for(db, "admin").get("/ai/usage?days=0").status_code == 422
