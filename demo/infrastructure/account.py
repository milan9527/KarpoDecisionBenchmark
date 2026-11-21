"""Create/reset the demo account without placing a password in argv or logs."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile

import boto3

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "demo/backend"))
from auth import hash_password

CREDENTIALS = ROOT / ".state/demo-login.json"


def new_account(username="demo", password=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", username):
        raise ValueError("Username must contain 1–64 letters, digits, dots, underscores or hyphens")
    password = secrets.token_urlsafe(24) if password is None else password
    return ({"username": username, "password": password},
            {"username": username, "password_hash": hash_password(password)})


def save_credentials(credentials):
    CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="demo-login-", dir=CREDENTIALS.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(credentials, output, indent=2)
            output.write("\n")
        os.replace(temporary, CREDENTIALS)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="demo")
    parser.add_argument("--prompt-password", action="store_true", help="Read a custom password without echo")
    args = parser.parse_args()
    password = None
    if args.prompt_password:
        password = getpass.getpass("New password (12–1024 characters): ")
        if password != getpass.getpass("Confirm password: "):
            parser.error("Passwords do not match")
    credentials, account = new_account(args.username, password)
    resources = json.loads((ROOT / "operations/demo-20261121/resources.json").read_text())
    sm = boto3.client("secretsmanager", region_name=resources["region"])
    value = json.loads(sm.get_secret_value(SecretId=resources["backend_secret"])["SecretString"])
    value.pop("access_code", None)
    value.update(account=account, cookie_key=secrets.token_urlsafe(48))
    sm.put_secret_value(SecretId=resources["backend_secret"], SecretString=json.dumps(value))
    save_credentials(credentials)
    print(f"Account {args.username} updated. Credentials: {CREDENTIALS} (0600).")
    print("Restart/install the API backend to activate the account and invalidate previous sessions.")


if __name__ == "__main__":
    main()
