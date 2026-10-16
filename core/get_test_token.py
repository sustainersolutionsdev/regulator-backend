"""
Test helper: a Firebase ID token for a test account (email/password sign-in).

T020 fix - Cloud Build failed on 2f064ba when Firebase Auth started refusing
sign-ins partway through the run: every auth_header() call signed in again,
about 150 sign-ins in ~5 minutes from one Cloud Build IP, and Firebase
throttled the rest. Now:
  - one sign-in per account per test run: the token is cached until shortly
    before it expires (ID tokens last 1 hour; the suite takes ~5 minutes);
  - a throttled sign-in (TOO_MANY_ATTEMPTS_TRY_LATER) waits and retries;
  - a failed sign-in raises with Firebase's own reason, and never with the
    request URL, which carries the API key (it used to land in build logs).
"""
from dotenv import load_dotenv
load_dotenv()
import os
import time
import requests

API_KEY = os.environ["FIREBASE_AUTH_KEY"].strip()
SIGN_IN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"

REFRESH_MARGIN_SECONDS = 300           # renew 5 minutes before expiry
RETRY_DELAYS_SECONDS = (2, 4, 8, 16)   # only for TOO_MANY_ATTEMPTS_TRY_LATER

_cache: dict[tuple[str, str], tuple[str, float]] = {}


class SignInError(RuntimeError):
    """Sign-in failed; the message carries Firebase's reason, not the URL."""


def _firebase_reason(resp) -> str:
    try:
        return resp.json().get("error", {}).get("message", "") or f"HTTP {resp.status_code}"
    except ValueError:
        return f"HTTP {resp.status_code}"


def _sign_in(email: str, password: str) -> tuple[str, float]:
    delays = list(RETRY_DELAYS_SECONDS)
    while True:
        resp = requests.post(
            SIGN_IN_URL,
            params={"key": API_KEY},
            json={"email": email, "password": password, "returnSecureToken": True},
            timeout=30,
        )
        if resp.ok:
            body = resp.json()
            expires_in = int(body.get("expiresIn", "3600"))
            return body["idToken"], time.monotonic() + expires_in - REFRESH_MARGIN_SECONDS
        reason = _firebase_reason(resp)
        if reason.startswith("TOO_MANY_ATTEMPTS_TRY_LATER") and delays:
            time.sleep(delays.pop(0))
            continue
        raise SignInError(f"Firebase sign-in failed for {email}: {reason}")


def get_id_token(email: str, password: str) -> str:
    key = (email, password)
    cached = _cache.get(key)
    if cached and time.monotonic() < cached[1]:
        return cached[0]
    token, expires_at = _sign_in(email, password)
    _cache[key] = (token, expires_at)
    return token


if __name__ == "__main__":
    token = get_id_token("admin@testtenant.com", os.environ["TEST_ACCOUNTS_PASSWORD"])
    print(token)
