"""
T015 - NFR-4 diagnosis: where does GET /regulations spend its time?
Times each stage in-process (no HTTP), so the slow stage is measured, not guessed.
Run with seed data present:  python tests\\perf_breakdown.py
"""
import sys, os, time, json
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from dotenv import load_dotenv
load_dotenv()

from fastapi.encoders import jsonable_encoder
from core.tenancy import db, list_regulations

TENANT = "test-tenant-001"

def timed(label, fn):
    t0 = time.perf_counter()
    out = fn()
    print(f"{label:<52} {time.perf_counter() - t0:6.2f}s")
    return out

tenant_ref = db.collection("tenants").document(TENANT)
timed("1. One Firestore round trip (warm-up)", lambda: tenant_ref.get())
timed("2. One Firestore round trip (network latency)", lambda: tenant_ref.get())
raw = timed("3. Stream all regulation docs (no to_dict)", lambda: list(tenant_ref.collection("regulations").stream()))
timed("4. Convert docs to Python dicts", lambda: [d.to_dict() for d in raw])
regs = timed("5. list_regulations() total (what the API calls)", lambda: list_regulations(TENANT))
enc = timed("6. FastAPI jsonable_encoder (default response path)", lambda: jsonable_encoder(regs))
timed("7. json.dumps of the encoded result", lambda: json.dumps(enc))
timed("8. json.dumps directly with default=str (alternative)", lambda: json.dumps(regs, default=str))
print(f"\nRecords: {len(regs)}")