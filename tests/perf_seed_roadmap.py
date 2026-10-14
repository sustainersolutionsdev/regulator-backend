"""
T015 - NFR-4 performance check for the Roadmap and calendar.

NFR-4: Roadmap/Dashboard render in under ~3 seconds at beta scale
(hundreds to low thousands of records per tenant).

Usage (backend running on :8000):
    python tests\\perf_seed_roadmap.py            # seed 1000, time GET /regulations
    python tests\\perf_seed_roadmap.py 2000       # different volume
    python tests\\perf_seed_roadmap.py --time     # time only, no seeding
Then time the screens in the browser (see the T015 steps), and remove the
seed data with:
    python tests\\cleanup_test_data.py --apply

Seed records are written straight to Firestore in batches (not through the
API) because they are throwaway load data, so they carry no audit-log
entries. Every title starts with "T015 Perf Regulation ", a prefix registered
in cleanup_test_data.py. Not collected by pytest (filename has no test_ prefix).
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

import random
import statistics
import time
from datetime import date, datetime, timedelta, timezone

import httpx
from core.get_test_token import get_id_token
from core.tenancy import db

BASE_URL = "http://127.0.0.1:8000"
TENANT_ID = "test-tenant-001"
TITLE_PREFIX = "T015 Perf Regulation "
DOMAINS = ["Batteries", "Chemicals", "Energy", "Packaging", "Recycling", "Climate", "Materials"]
REGIONS = ["", "Federal", "California", "New York", "EU-wide"]


def seed(count: int) -> None:
    tenant_ref = db.collection("tenants").document(TENANT_ID)
    now = datetime.now(timezone.utc)
    start = date(date.today().year, 1, 1)
    batch, pending = db.batch(), 0
    for i in range(count):
        due = start + timedelta(days=random.randint(0, 5 * 365))
        ref = tenant_ref.collection("regulations").document()
        batch.set(ref, {
            "title": f"{TITLE_PREFIX}{i:04d}",
            "abbreviatedTitle": f"PERF {i:04d}",
            "dueDate": due.isoformat(),
            "domain": random.choice(DOMAINS),
            "country": random.choice(["US", "US", "US", "EU"]),
            "region": random.choice(REGIONS),
            "businessUnitIds": random.choice([["BU1"], ["BU2"], ["BU1", "BU2"]]),
            "notes": "",
            "documents": [],
            "addToRoadmapDashboard": True,
            "createdAt": now,
            "lastUpdated": now,
            "createdBy": "perf-seed",
            "hidden": False,
        })
        pending += 1
        if pending == 450:
            batch.commit()
            batch, pending = db.batch(), 0
    if pending:
        batch.commit()
    print(f"[perf] seeded {count} regulations into {TENANT_ID}")


def time_api(runs: int = 5) -> None:
    token = get_id_token("admin@testtenant.com", os.environ["TEST_ACCOUNTS_PASSWORD"].strip())
    headers = {"Authorization": f"Bearer {token}"}
    timings, size, count = [], 0, 0
    for _ in range(runs):
        t0 = time.perf_counter()
        resp = httpx.get(f"{BASE_URL}/regulations", headers=headers, timeout=60.0)
        timings.append(time.perf_counter() - t0)
        resp.raise_for_status()
        size, count = len(resp.content), len(resp.json())
    print(f"[perf] GET /regulations: {count} records, {size / 1024:.0f} KB")
    print(f"[perf]   median {statistics.median(timings):.2f}s, worst {max(timings):.2f}s over {runs} runs")
    verdict = "PASS" if max(timings) < 3.0 else "OVER BUDGET"
    print(f"[perf]   API share of the 3s NFR-4 budget: {verdict}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--time" not in args:
        n = int(args[0]) if args and args[0].isdigit() else 1000
        seed(n)
    time_api()
