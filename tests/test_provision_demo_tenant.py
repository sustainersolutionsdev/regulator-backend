"""tools/provision_demo_tenant.py - argument checks (no Firestore or Firebase needed)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "tools")))
import provision_demo_tenant as demo  # noqa: E402


def plan(*argv):
    p = demo.argparse.ArgumentParser()
    p.add_argument("--tenant-id"); p.add_argument("--name")
    p.add_argument("--bu", action="append"); p.add_argument("--user", action="append")
    return demo.build_plan(p.parse_args(list(argv)))


def test_full_plan():
    pl = plan("--tenant-id", "sustainer-demo", "--name", "Sustainer Demo",
              "--bu", "BU1=Printers", "--bu", "bu2 = Data Centers",
              "--user", "A.Person@Example.com|A Person|Admin",
              "--user", "a.person+user@example.com|A Person (User view)|user|BU1, bu2")
    assert pl["bus"] == {"BU1": "Printers", "BU2": "Data Centers"}
    assert pl["users"][0] == {"email": "a.person@example.com", "name": "A Person", "role": "admin", "bus": []}
    assert pl["users"][1]["bus"] == ["BU1", "BU2"]


@pytest.mark.parametrize("argv, message", [
    (["--tenant-id", "Bad Name"], "--tenant-id"),
    (["--tenant-id", "demo-x", "--bu", "BU2"], "CODE=Label"),
    (["--tenant-id", "demo-x", "--user", "not-an-email|X|admin"], "not an email"),
    (["--tenant-id", "demo-x", "--user", "a@b.co|X|owner"], "role must be"),
    (["--tenant-id", "demo-x", "--user", "a@b.co|X|user"], "at least one Business Unit"),
    (["--tenant-id", "demo-x", "--user", "a@b.co|X|user|BU9"], "unknown Business Unit"),
    (["--tenant-id", "demo-x", "--user", "a@b.co|X|admin", "--user", "A@B.co|Y|sme"], "twice"),
])
def test_bad_input_is_refused(argv, message):
    with pytest.raises(ValueError, match=message):
        plan(*argv)


def test_default_business_unit():
    assert plan("--tenant-id", "demo-x")["bus"] == {"BU1": "Default"}
