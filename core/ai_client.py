"""
core/ai_client.py - the one place Regulator calls Gemini (T028, M4).

Every AI feature (summaries, actions needed, Insights, graph summaries, and
later the M5 duplicate check and classification help) goes through
generate() / generate_json(). That gives one place for:

  - the model: GEMINI_MODEL (default gemini-3.1-flash-lite). Switching model
    is a setting, not a code change.
  - the key: GEMINI_API_KEY. Locally from .env; on Cloud Run from Secret
    Manager (gemini-api-key), never baked into an image (TAD 13.1).
  - cost control (NFR-7, RISK-2): every call is written to the company's AI
    usage log with tokens and an estimated cost, and each company has a
    daily call limit (AI_DAILY_CALL_LIMIT, default 300), so a bug or a
    burst of use cannot drain the prepaid balance.
  - plain-language errors the API can return to the screen: AI not set up,
    the prepaid credit ran out, today's limit reached, no answer.

Uses the Gemini REST API over httpx (already a dependency) rather than the
google-genai SDK: no new packages, nothing extra loaded at start-up (the
Oct 24 cold-start work), and the request is fully visible in one place.

Privacy: the usage log records counts, model, feature and who asked -
never the prompt or the answer. Prompts and answers stay in the request.
Gemini paid tier: prompts and responses are not used to improve Google's
products (checked for the Security FAQ, Oct 23).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from dataclasses import dataclass

import httpx

import core.config  # noqa: F401  (loads .env for local runs)

logger = logging.getLogger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.1-flash-lite"

# List prices for the default model, USD per 1M tokens (checked Nov 10, 2026).
# Used only to ESTIMATE cost in the usage log; the billed amount is in AI
# Studio. Override per model with GEMINI_PRICE_INPUT_PER_M / _OUTPUT_PER_M.
DEFAULT_PRICE_INPUT_PER_M = 0.25
DEFAULT_PRICE_OUTPUT_PER_M = 1.50


# --- errors -------------------------------------------------------------------

class AIError(Exception):
    """Base AI failure. `message` is safe to show on screen."""
    code = "ai_unavailable"
    http_status = 503
    message = "The AI service isn't available right now. Please try again in a few minutes."

    def __init__(self, detail: str = ""):
        super().__init__(detail or self.message)
        self.detail = detail


class AINotConfigured(AIError):
    code = "not_configured"
    message = "AI isn't set up on this server yet."


class AICreditsExhausted(AIError):
    code = "credits_exhausted"
    message = "AI is paused for now. Please contact your administrator."


class AILimitReached(AIError):
    code = "daily_limit"
    http_status = 429
    message = "Your company has reached today's AI limit. It resets at midnight UTC."


class AINoAnswer(AIError):
    code = "no_answer"
    http_status = 502
    message = "The AI couldn't produce an answer for this. Please try again."


@dataclass
class AIResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int  # includes thinking tokens, which are billed as output
    cost_usd: float
    finish_reason: str


# --- settings (read at call time, so tests and deploys can change them) -------

def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def settings() -> dict:
    return {
        "api_key": (os.environ.get("GEMINI_API_KEY") or "").strip(),
        "model": (os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL).strip(),
        "timeout": _float_env("GEMINI_TIMEOUT_SECONDS", 45.0),
        "daily_limit": _int_env("AI_DAILY_CALL_LIMIT", 300),
        "price_in": _float_env("GEMINI_PRICE_INPUT_PER_M", DEFAULT_PRICE_INPUT_PER_M),
        "price_out": _float_env("GEMINI_PRICE_OUTPUT_PER_M", DEFAULT_PRICE_OUTPUT_PER_M),
        # Optional: e.g. "low" to keep thinking tokens (billed as output) down.
        # Left unset by default, because not every model accepts it.
        "thinking_level": (os.environ.get("GEMINI_THINKING_LEVEL") or "").strip(),
    }


def is_enabled() -> bool:
    return bool(settings()["api_key"])


# --- seams for tests ----------------------------------------------------------

def _db():
    from core.clients import db
    return db


def _increment(value):
    from google.cloud import firestore
    return firestore.Increment(value)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


_sleep = time.sleep


# --- usage log ----------------------------------------------------------------

def _tenant(db, tenant_id: str):
    return db.collection("tenants").document(tenant_id)


def calls_today(tenant_id: str, *, db=None, now: dt.datetime | None = None) -> int:
    day = (now or _utcnow()).strftime("%Y-%m-%d")
    snap = _tenant(db or _db(), tenant_id).collection("aiUsageDaily").document(day).get()
    return int(((snap.to_dict() or {}) if snap.exists else {}).get("calls", 0))


def _record(db, tenant_id: str, now: dt.datetime, entry: dict, billed: bool) -> None:
    """Writes one usage entry and, if Gemini billed the call, adds it to the
    day's totals. Never raises: a logging failure must not lose an answer."""
    try:
        t = _tenant(db, tenant_id)
        t.collection("aiUsage").add(entry)
        if billed:
            day = now.strftime("%Y-%m-%d")
            t.collection("aiUsageDaily").document(day).set({
                "date": day,
                "calls": _increment(1),
                "inputTokens": _increment(entry["inputTokens"]),
                "outputTokens": _increment(entry["outputTokens"]),
                "costUsd": _increment(entry["costUsd"]),
                "lastAt": now,
            }, merge=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("AI usage log write failed for tenant %s: %s", tenant_id, type(e).__name__)


# --- request ------------------------------------------------------------------

def _parts(contents) -> list[dict]:
    """A string, or a list of strings / Gemini parts (e.g. a document as
    {"inline_data": {"mime_type": "application/pdf", "data": <base64>}})."""
    if isinstance(contents, str):
        return [{"text": contents}]
    parts = []
    for item in contents:
        parts.append({"text": item} if isinstance(item, str) else item)
    return parts


def _build_body(contents, system, max_output_tokens, temperature, json_mode, thinking_level) -> dict:
    generation = {"maxOutputTokens": max_output_tokens, "temperature": temperature}
    if json_mode:
        generation["responseMimeType"] = "application/json"
    if thinking_level:
        generation["thinkingConfig"] = {"thinkingLevel": thinking_level}
    body = {"contents": [{"role": "user", "parts": _parts(contents)}], "generationConfig": generation}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    return body


def _error_text(resp) -> str:
    try:
        return str(resp.json().get("error", {}).get("message", ""))[:300]
    except Exception:  # noqa: BLE001
        return (resp.text or "")[:300]


def _is_credit_problem(status: int, text: str) -> bool:
    t = text.lower()
    return status == 402 or (status == 429 and ("prepay" in t or "credit" in t or "billing" in t))


def _send(post, url: str, headers: dict, body: dict, timeout: float):
    """One retry for a dropped connection, a timeout, a 5xx or a rate limit.
    A credit or request problem is never retried."""
    last = ""
    for attempt in (1, 2):
        try:
            resp = post(url, headers=headers, json=body, timeout=timeout)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last = type(e).__name__
            if attempt == 1:
                _sleep(1.0)
                continue
            raise AIError(f"network: {last}")
        status = resp.status_code
        if status == 200:
            return resp
        text = _error_text(resp)
        if _is_credit_problem(status, text):
            raise AICreditsExhausted(f"{status}: {text}")
        if status == 429 or status >= 500:
            last = f"{status}: {text}"
            if attempt == 1:
                _sleep(2.0)
                continue
            raise AIError(last)
        # 400 / 403 / 404: a bad request, a bad key or an unknown model.
        raise AIError(f"{status}: {text}")
    raise AIError(last)  # pragma: no cover


def _parse(data: dict) -> tuple[str, str, dict]:
    candidates = data.get("candidates") or []
    finish = ""
    text = ""
    if candidates:
        first = candidates[0]
        finish = str(first.get("finishReason") or "")
        parts = (first.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    return text.strip(), finish, data.get("usageMetadata") or {}


def generate(
    tenant_id: str,
    feature: str,
    contents,
    *,
    system: str | None = None,
    max_output_tokens: int = 1024,
    temperature: float = 0.2,
    json_mode: bool = False,
    actor_uid: str | None = None,
    db=None,
    post=None,
    now: dt.datetime | None = None,
) -> AIResult:
    """Calls Gemini for one company and one feature (e.g. "regulation_summary").
    Raises an AIError subclass whose .message can be shown on screen."""
    s = settings()
    if not s["api_key"]:
        raise AINotConfigured()
    db = db or _db()
    post = post or httpx.post
    now = now or _utcnow()

    entry = {
        "at": now, "feature": feature, "model": s["model"], "uid": actor_uid or "",
        "status": "ok", "errorCode": "", "inputTokens": 0, "outputTokens": 0,
        "thoughtsTokens": 0, "cachedTokens": 0, "costUsd": 0.0, "latencyMs": 0,
    }

    if calls_today(tenant_id, db=db, now=now) >= s["daily_limit"]:
        _record(db, tenant_id, now, {**entry, "status": "limited", "errorCode": AILimitReached.code}, billed=False)
        raise AILimitReached()

    body = _build_body(contents, system, max_output_tokens, temperature, json_mode, s["thinking_level"])
    url = f"{API_ROOT}/models/{s['model']}:generateContent"
    headers = {"x-goog-api-key": s["api_key"], "Content-Type": "application/json"}

    started = time.monotonic()
    try:
        resp = _send(post, url, headers, body, s["timeout"])
    except AIError as e:
        logger.warning("Gemini call failed (%s, %s): %s", feature, e.code, e.detail)
        _record(db, tenant_id, now, {**entry, "status": "error", "errorCode": e.code,
                                     "latencyMs": int((time.monotonic() - started) * 1000)}, billed=False)
        raise

    text, finish, usage = _parse(resp.json())
    tokens_in = int(usage.get("promptTokenCount") or 0)
    thoughts = int(usage.get("thoughtsTokenCount") or 0)
    tokens_out = int(usage.get("candidatesTokenCount") or 0) + thoughts
    cost = round(tokens_in * s["price_in"] / 1e6 + tokens_out * s["price_out"] / 1e6, 6)
    entry.update({
        "inputTokens": tokens_in, "outputTokens": tokens_out, "thoughtsTokens": thoughts,
        "cachedTokens": int(usage.get("cachedContentTokenCount") or 0), "costUsd": cost,
        "latencyMs": int((time.monotonic() - started) * 1000),
    })

    if not text:
        _record(db, tenant_id, now, {**entry, "status": "no_answer", "errorCode": finish or "EMPTY"}, billed=True)
        raise AINoAnswer(f"finishReason={finish or 'none'}")

    _record(db, tenant_id, now, entry, billed=True)
    return AIResult(text=text, model=s["model"], input_tokens=tokens_in, output_tokens=tokens_out,
                    cost_usd=cost, finish_reason=finish)


def generate_json(tenant_id: str, feature: str, contents, **kwargs) -> tuple[dict | list, AIResult]:
    """generate() with a JSON answer, parsed. A reply that isn't valid JSON
    raises AINoAnswer (the call is still logged and counted)."""
    result = generate(tenant_id, feature, contents, json_mode=True, **kwargs)
    text = result.text
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1:] if "\n" in text else text
    try:
        return json.loads(text), result
    except ValueError:
        raise AINoAnswer("reply was not valid JSON")
