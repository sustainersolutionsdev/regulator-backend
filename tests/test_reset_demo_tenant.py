"""T078: tools/reset_demo_tenant.py - refusals, inventory, backup and delete order, with fakes (no Google)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "tools")))

import reset_demo_tenant as rt  # noqa: E402


# --- refusals ------------------------------------------------------------------

def test_refuses_other_projects():
    with pytest.raises(rt.Refused, match="regulator-dev"):
        rt.check_allowed("regulator-beta", "sustainer-demo", False, None, [])


def test_refuses_the_automated_test_tenant_and_test_accounts():
    with pytest.raises(rt.Refused, match="protected"):
        rt.check_allowed("regulator-dev", "test-tenant-001", False, None, [])
    with pytest.raises(rt.Refused, match="automated-test accounts"):
        rt.check_allowed("regulator-dev", "other", False, None, ["admin@testtenant.com"])
    with pytest.raises(rt.Refused, match="automated-test accounts"):
        rt.check_allowed("regulator-dev", "other", False, None, ["Admin@TenantB.com"])


def test_apply_needs_the_tenant_id_typed_again():
    rt.check_allowed("regulator-dev", "sustainer-demo", False, None, ["a@sustainer.example"])  # dry run: fine
    with pytest.raises(rt.Refused, match="--confirm sustainer-demo"):
        rt.check_allowed("regulator-dev", "sustainer-demo", True, None, [])
    with pytest.raises(rt.Refused):
        rt.check_allowed("regulator-dev", "sustainer-demo", True, "sustainer", [])
    rt.check_allowed("regulator-dev", "sustainer-demo", True, "sustainer-demo", [])


# --- fakes ---------------------------------------------------------------------

class Ref:
    def __init__(self, path, tree):
        self.path, self.tree = path, tree
        self.id = path.split("/")[-1]

    @property
    def parent(self):
        return Coll("/".join(self.path.split("/")[:-1]), self.tree)

    def collections(self):
        names = sorted({p[len(self.path) + 1:].split("/")[0] for p in self.tree if p.startswith(self.path + "/")})
        return [Coll(f"{self.path}/{n}", self.tree) for n in names]

    def get(self):
        return Snap(self, self.tree.get(self.path))


class Coll:
    def __init__(self, path, tree):
        self.path, self.tree = path, tree
        self.id = path.split("/")[-1]

    @property
    def parent(self):
        return Ref("/".join(self.path.split("/")[:-1]), self.tree)

    def stream(self):
        depth = self.path.count("/") + 1
        return [Snap(Ref(p, self.tree), d) for p, d in sorted(self.tree.items())
                if p.startswith(self.path + "/") and p.count("/") == depth]

    def document(self, doc_id):
        return Ref(f"{self.path}/{doc_id}", self.tree)


class Snap:
    def __init__(self, ref, data):
        self.reference, self._data = ref, data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class Batch:
    def __init__(self, db):
        self.db, self.ops = db, []

    def delete(self, ref):
        self.ops.append(ref.path)

    def commit(self):
        self.db.deleted.extend(self.ops)
        for p in self.ops:
            self.db.tree.pop(p, None)


class DB:
    def __init__(self, tree):
        self.tree, self.deleted = tree, []

    def collection(self, name):
        return Coll(name, self.tree)

    def batch(self):
        return Batch(self)


class User:
    def __init__(self, uid, email, tenant):
        self.uid, self.email, self.display_name = uid, email, uid
        self.custom_claims = {"tenant_id": tenant, "role": "admin"} if tenant else None


class Auth:
    class UserNotFoundError(Exception):
        pass

    def __init__(self, users):
        self.users, self.deleted = users, []

    def list_users(self):
        users = self.users

        class Page:
            def iterate_all(self):
                return list(users)
        return Page()

    def delete_user(self, uid):
        self.deleted.append(uid)


class Blob:
    def __init__(self, name, bucket):
        self.name, self.bucket = name, bucket

    def download_to_filename(self, dest):
        with open(dest, "w") as fh:
            fh.write("file " + self.name)

    def delete(self):
        self.bucket.deleted.append(self.name)


class Bucket:
    def __init__(self, names):
        self.names, self.deleted = names, []

    def list_blobs(self, prefix):
        return [Blob(n, self) for n in self.names if n.startswith(prefix)]


def world():
    tree = {
        "tenants/sustainer-demo": {"name": "Sustainer Demo"},
        "tenants/sustainer-demo/businessUnits/BU1": {"label": "Printers"},
        "tenants/sustainer-demo/regulations/r1": {"title": "EU Battery Regulation"},
        "tenants/sustainer-demo/regulations/r1/activity/a1": {"note": "supplier data"},
        "tenants/sustainer-demo/users/u2": {"email": "listed@x.example"},
        "tenants/sustainer-demo/aiSummaries/r1": {"short": "..."},
        "tenants/test-tenant-001": {"name": "Test"},
        "tenants/test-tenant-001/regulations/t1": {"title": "keep me"},
    }
    users = [User("u1", "srinivas@sustainer.example", "sustainer-demo"), User("u2", "listed@x.example", None),
             User("u3", "admin@testtenant.com", "test-tenant-001")]
    return DB(tree), Auth(users), Bucket(["tenants/sustainer-demo/regulation-documents/f.pdf",
                                          "tenants/test-tenant-001/regulation-documents/g.pdf"])


def test_inventory_finds_every_subcollection_login_and_file_of_one_tenant_only():
    db, auth, bucket = world()
    info = rt.summary(rt.inventory(db, auth, bucket, "sustainer-demo"))
    assert info["documents_by_collection"] == {"activity": 1, "aiSummaries": 1, "businessUnits": 1, "regulations": 1, "users": 1}
    assert info["documents_total"] == 6
    assert info["logins"] == ["listed@x.example", "srinivas@sustainer.example"]
    assert info["uploaded_files"] == 1


def test_backup_then_delete_leaves_other_tenants_untouched(tmp_path):
    db, auth, bucket = world()
    inv = rt.inventory(db, auth, bucket, "sustainer-demo")
    target = rt.backup(inv, "sustainer-demo", tmp_path)
    lines = [json.loads(x) for x in (target / "firestore.jsonl").read_text().splitlines()]
    assert len(lines) == 6 and lines[0]["path"] == "tenants/sustainer-demo"
    assert {u["email"] for u in json.loads((target / "logins.json").read_text())} == {"srinivas@sustainer.example", "listed@x.example"}
    assert len(list((target / "files").iterdir())) == 1

    rt.delete_all(db, auth, inv)
    assert sorted(auth.deleted) == ["u1", "u2"]
    assert bucket.deleted == ["tenants/sustainer-demo/regulation-documents/f.pdf"]
    assert db.deleted.index("tenants/sustainer-demo/regulations/r1/activity/a1") < db.deleted.index("tenants/sustainer-demo/regulations/r1")
    assert db.deleted[-1] == "tenants/sustainer-demo"
    assert set(db.tree) == {"tenants/test-tenant-001", "tenants/test-tenant-001/regulations/t1"}
    assert rt.summary(rt.inventory(db, auth, bucket, "sustainer-demo"))["documents_total"] == 0
