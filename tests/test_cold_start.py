"""
Oct 23: regulator-dev runs with no warm server (min instances 0) to keep dev
and demo costs at zero; these keep the start-up work small.

The checks that import main.py run in a fresh Python process with Google's
credential look-up replaced by anonymous credentials, so they need no key
file, network or Firestore, and see exactly what a new server would load.
"""
import pathlib
import subprocess
import sys
import textwrap

ROOT = pathlib.Path(__file__).resolve().parent.parent

FAKE_CREDENTIALS = """
    import os, google.auth
    from google.auth.credentials import AnonymousCredentials
    os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    os.environ["GCP_PROJECT"] = "regulator-dev"
    google.auth.default = lambda *a, **k: (AnonymousCredentials(), "regulator-dev")
"""


def run_fresh(body: str) -> str:
    code = textwrap.dedent(FAKE_CREDENTIALS) + textwrap.dedent(body)
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    return result.stdout


def test_server_starts_without_loading_cloud_storage():
    out = run_fresh("""
        import sys
        import main
        print("storage" if "google.cloud.storage" in sys.modules else "no-storage")
    """)
    assert out.strip().endswith("no-storage")


def test_health_answers_without_a_token_and_reads_no_data():
    out = run_fresh("""
        from fastapi.testclient import TestClient
        import core.clients, main

        class NoData:
            def __getattr__(self, name):
                raise AssertionError("health must not touch Firestore")
        core.clients.db = NoData()
        r = TestClient(main.app).get("/health")
        body = r.json()
        print(r.status_code, body["status"], body["version"], body["commit"])
    """)
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert out.strip().endswith(f"200 ok {version} local")


def test_health_is_allowed_from_the_web_app():
    out = run_fresh("""
        import os
        os.environ["CORS_ORIGINS"] = "https://regulator-dev.web.app"
        from fastapi.testclient import TestClient
        import main
        r = TestClient(main.app).get("/health", headers={"Origin": "https://regulator-dev.web.app"})
        print(r.headers.get("access-control-allow-origin"))
    """)
    assert out.strip().endswith("https://regulator-dev.web.app")


def test_all_modules_share_one_firestore_client():
    out = run_fresh("""
        import core.audit, core.clients, core.deps, core.tenancy, core.users
        dbs = {id(m.db) for m in (core.audit, core.clients, core.deps, core.tenancy, core.users)}
        print(len(dbs))
    """)
    assert out.strip().endswith("1")


def test_storage_client_is_created_once_on_first_use(monkeypatch):
    from google.cloud import storage as gcs

    from core import storage

    made = []

    class FakeClient:
        def __init__(self, project=None):
            made.append(project)

    monkeypatch.setattr(gcs, "Client", FakeClient)
    monkeypatch.setattr(storage, "_client", None)
    first = storage.storage_client()
    assert storage.storage_client() is first
    assert len(made) == 1


def test_dev_deploy_keeps_no_warm_api_and_at_most_one_instance():
    text = (ROOT / "cloudbuild.deploy-dev.yaml").read_text(encoding="utf-8")
    assert '_API_MIN_INSTANCES: "0"' in text
    assert '_API_MAX_INSTANCES: "1"' in text
    assert "--min-instances=${_API_MIN_INSTANCES}" in text
    assert "--max-instances=${_API_MAX_INSTANCES}" in text
    # The Alerts Worker never keeps a warm server: a late start only delays an email.
    worker = text.split("id: deploy-alerts-worker", 1)[1]
    assert "--min-instances=0" in worker


def test_image_compiles_app_code_at_build_time():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "python -m compileall -q core main.py worker.py" in text
    assert "COPY main.py worker.py VERSION ./" in text
