"""
pytest session hooks for regulator-backend.

After the whole test session finishes - pass or fail, locally or in Cloud
Build - delete the records the suite created in the test tenants
(see tests/cleanup_test_data.py for exactly what is matched).

Set KEEP_TEST_DATA=1 to skip the cleanup when you need to inspect what a
test wrote:
    $env:KEEP_TEST_DATA = "1"; pytest tests/ -v; Remove-Item Env:KEEP_TEST_DATA
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_data_after_session():
    yield
    if os.environ.get("KEEP_TEST_DATA") == "1":
        print("\n[cleanup] KEEP_TEST_DATA=1 - test data left in place.")
        return
    from cleanup_test_data import run_cleanup

    try:
        totals = run_cleanup(apply=True, verbose=False)
        print(f"\n[cleanup] removed test data: {totals}")
    except Exception as e:  # cleanup must never turn a green run red
        print(f"\n[cleanup] WARNING: test-data cleanup failed: {type(e).__name__}: {e}")
