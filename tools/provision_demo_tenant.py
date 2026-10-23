"""
tools/provision_demo_tenant.py - create a company workspace (tenant) with its
Business Units and first users on regulator-dev, for demos and testing
(OPEN-11 point 4, interim; the onboarding runbook comes in late December).

Uses only the sanctioned paths: core.tenancy.provision_tenant /
create_business_unit and core.users.create_user. No passwords or reset links
are printed: each person sets their own password with "Forgot password?" on
the Login screen at https://regulator-dev.web.app.

Dry run by default - shows what it would do. Add --apply to do it.
Safe to run again: an existing tenant, Business Unit or login is left alone.

Usage (PowerShell, backend venv active, from the regulator-backend folder):

  python tools\\provision_demo_tenant.py `
    --tenant-id sustainer-demo --name "Sustainer Demo" `
    --bu "BU1=Printers" --bu "BU2=Computers" --bu "BU3=Data Centers" `
    --user "first.person@company.com|First Person|admin" `
    --user "first.person+user@company.com|First Person (User view)|user|BU1"

  --user is "email|display name|role[|BU codes, comma-separated]".
  Roles: admin, sme, user. Admin/SME always get every Business Unit; a user
  needs at least one.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

ROLES = {"admin", "sme", "user"}
TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")
BU_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,19}$")
EMAIL_RE = re.compile(r"^[^@\s|]+@[^@\s|]+\.[^@\s|]+$")


def parse_bu(text: str) -> tuple[str, str]:
    code, sep, label = text.partition("=")
    code, label = code.strip().upper(), label.strip()
    if not sep or not BU_CODE_RE.match(code) or not label:
        raise ValueError(f'--bu "{text}": expected CODE=Label, e.g. "BU2=Data Centers"')
    return code, label


def parse_user(text: str, bu_codes: set[str]) -> dict:
    parts = [p.strip() for p in text.split("|")]
    if len(parts) not in (3, 4):
        raise ValueError(f'--user "{text}": expected "email|display name|role[|BU1,BU2]"')
    email, name, role = parts[0].lower(), parts[1], parts[2].lower()
    if not EMAIL_RE.match(email):
        raise ValueError(f'--user "{text}": "{email}" is not an email address')
    if role not in ROLES:
        raise ValueError(f'--user "{text}": role must be one of {sorted(ROLES)}')
    bus = [b.strip().upper() for b in parts[3].split(",") if b.strip()] if len(parts) == 4 else []
    if role == "user" and not bus:
        raise ValueError(f'--user "{text}": a user needs at least one Business Unit')
    unknown = [b for b in bus if b not in bu_codes]
    if unknown:
        raise ValueError(f'--user "{text}": unknown Business Unit(s) {unknown}; add them with --bu')
    return {"email": email, "name": name or email, "role": role, "bus": bus}


def build_plan(args) -> dict:
    if not TENANT_ID_RE.match(args.tenant_id):
        raise ValueError("--tenant-id: 3-40 characters, lowercase letters, digits and hyphens, e.g. sustainer-demo")
    bus = dict(parse_bu(b) for b in (args.bu or ["BU1=Default"]))
    users = [parse_user(u, set(bus)) for u in (args.user or [])]
    emails = [u["email"] for u in users]
    if len(emails) != len(set(emails)):
        raise ValueError("the same email appears twice")
    return {"tenant_id": args.tenant_id, "name": args.name or args.tenant_id, "bus": bus, "users": users}


def run(plan: dict, apply: bool) -> int:
    from firebase_admin import auth
    from core.config import GCP_PROJECT
    from core.tenancy import create_business_unit, db, provision_tenant
    from core.users import create_user, _generate_temp_password

    if GCP_PROJECT != "regulator-dev":
        print(f"Refusing: this tool is for regulator-dev, but GCP_PROJECT is '{GCP_PROJECT}'.")
        return 2
    mode = "APPLY" if apply else "DRY RUN (nothing written; add --apply)"
    print(f"[demo] {mode} - project {GCP_PROJECT}, tenant '{plan['tenant_id']}'")
    tid = plan["tenant_id"]
    tenant_ref = db.collection("tenants").document(tid)

    # 1. Tenant (provision_tenant also creates BU1 "Default")
    tenant_existed = tenant_ref.get().exists
    if tenant_existed:
        print(f"[demo] tenant '{tid}' already exists - kept")
    else:
        print(f"[demo] create tenant '{tid}' ({plan['name']})")
        if apply:
            provision_tenant(tid, plan["name"])

    # 2. Business Units
    for code, label in plan["bus"].items():
        ref = tenant_ref.collection("businessUnits").document(code)
        if code == "BU1" and not tenant_existed:
            # provision_tenant has just created BU1 as "Default": give it this label.
            print(f"[demo] Business Unit BU1 '{label}'")
            if apply:
                ref.set({"label": label}, merge=True)
        elif tenant_existed and ref.get().exists:
            print(f"[demo] Business Unit {code} already exists - kept")
        else:
            print(f"[demo] create Business Unit {code} '{label}'")
            if apply:
                create_business_unit(tid, code, label)

    # 3. Users
    created = 0
    for u in plan["users"]:
        try:
            auth.get_user_by_email(u["email"])
            print(f"[demo] {u['email']}: a login already exists - skipped (change it in the app, or remove it first)")
            continue
        except auth.UserNotFoundError:
            pass
        scope = "all Business Units" if u["role"] in ("admin", "sme") else ", ".join(u["bus"])
        print(f"[demo] add {u['email']} ({u['name']}) as {u['role']} - {scope}")
        if apply:
            create_user(u["email"], _generate_temp_password(), tid, u["role"], u["bus"],
                        display_name=u["name"], notes="Demo/testing account (regulator-dev)")
            created += 1

    print(f"[demo] done - {created} login(s) created." if apply else "[demo] dry run complete.")
    if apply and created:
        print("[demo] Next: each person opens https://regulator-dev.web.app, clicks 'Forgot password?',")
        print("       enters their email and sets a password from the reset email.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant-id", required=True)
    p.add_argument("--name")
    p.add_argument("--bu", action="append", help='CODE=Label, repeatable (e.g. "BU2=Data Centers")')
    p.add_argument("--user", action="append", help='"email|display name|role[|BU1,BU2]", repeatable')
    p.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    args = p.parse_args(argv)
    try:
        plan = build_plan(args)
    except ValueError as e:
        print(f"Error: {e}")
        return 2
    return run(plan, args.apply)


if __name__ == "__main__":
    sys.exit(main())
