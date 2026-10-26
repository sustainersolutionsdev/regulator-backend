"""
core/config.py - per-environment settings (OPEN-11 points 1 and 3).

Until the first deploy, the GCP project ("regulator-dev"), the documents
bucket and the CORS origin (localhost:5173) were written into five modules.
They now come from the environment, with the old values as defaults, so:
  - local development and Cloud Build tests behave exactly as before
    (no .env change needed);
  - the deployed API on Cloud Run gets its values from the service's
    environment (set by cloudbuild.deploy-dev.yaml);
  - regulator-beta, when it is created, is the same image with different
    values - no code change (TAD 12.1).

Firebase Admin initialisation lives here too. Locally it uses the service
account key named by GOOGLE_APPLICATION_CREDENTIALS, as before. On Cloud Run
there is no key file: it uses the service's own identity (Application
Default Credentials), so no key is ever baked into an image (TAD 13.1).
"""
import os

from dotenv import load_dotenv

load_dotenv()

GCP_PROJECT = (os.environ.get("GCP_PROJECT") or "regulator-dev").strip()
DOCUMENTS_BUCKET = (os.environ.get("DOCUMENTS_BUCKET") or f"{GCP_PROJECT}-documents").strip()

DEFAULT_CORS_ORIGINS = "http://localhost:5173"

# T058 versioning (Oct 26): the release number lives in the VERSION file at
# the repo root (copied into the image); the deploy adds the commit
# (APP_COMMIT). GET /health reports both, so any running server can be
# matched to its release tag (v<version>) and its exact commit.
def _read_version() -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


APP_VERSION = _read_version()
APP_COMMIT = (os.environ.get("APP_COMMIT") or "local").strip()


def cors_origins() -> list[str]:
    """Comma-separated CORS_ORIGINS, e.g.
    "https://regulator-dev.web.app,https://regulator-dev.firebaseapp.com,http://localhost:5173"."""
    raw = os.environ.get("CORS_ORIGINS") or DEFAULT_CORS_ORIGINS
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


def init_firebase() -> None:
    """Initialise the Firebase Admin SDK once per process."""
    import firebase_admin
    from firebase_admin import credentials

    if firebase_admin._apps:
        return
    key_file = (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    cred = credentials.Certificate(key_file) if key_file else credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred, {"projectId": GCP_PROJECT})
