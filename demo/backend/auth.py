"""Password verification and signed, expiring demo sessions; no AWS dependencies."""
import base64
import hashlib
import hmac
import json
import secrets
import threading
import time

ITERATIONS = 600_000
COOKIE_SECONDS = 28_800


def hash_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ValueError("Password must contain 12–1024 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_credentials(username, password, account):
    if not isinstance(username, str) or not isinstance(password, str):
        return False
    if not 1 <= len(username) <= 64 or not 1 <= len(password) <= 1024:
        return False
    try:
        algorithm, iterations, salt, expected = account["password_hash"].split("$")
        if algorithm != "pbkdf2_sha256" or int(iterations) != ITERATIONS:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
        # Derive for unknown usernames too; do not reveal account existence.
        password_ok = hmac.compare_digest(actual, bytes.fromhex(expected))
        username_ok = hmac.compare_digest(username.encode(), account["username"].encode())
        return password_ok and username_ok
    except (KeyError, ValueError, TypeError, UnicodeError):
        return False


def issue_session(session, key, username, now=None):
    now = time.time() if now is None else now
    body = base64.urlsafe_b64encode(json.dumps({
        "v": 1, "session": session, "username": username, "exp": int(now) + COOKIE_SECONDS,
    }).encode()).decode()
    signature = hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()
    return body + "." + signature


def read_session(cookie, key, username, now=None):
    now = time.time() if now is None else now
    try:
        if not isinstance(cookie, str) or len(cookie) > 4096:
            raise ValueError()
        body, signature = cookie.split(".")
        expected = hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError()
        value = json.loads(base64.urlsafe_b64decode(body))
        if (not isinstance(value, dict) or value.get("v") != 1 or
                value.get("username") != username or type(value.get("exp")) is not int or
                value["exp"] <= now or not isinstance(value.get("session"), str) or
                len(value["session"]) != 32):
            raise ValueError()
        return value["session"]
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("Invalid or expired session") from None


def viewer_ip(forwarded, fallback):
    # The only ingress is CloudFront -> ALB. Both append to X-Forwarded-For:
    # [...untrusted viewer-supplied entries, viewer IP, CloudFront origin IP].
    chain = forwarded.split(",")
    return chain[-2].strip() if len(chain) >= 2 else fallback


class LoginLimiter:
    """Single API process: 8 attempts/IP/minute and 60 total/minute."""
    def __init__(self):
        self.attempts = []
        self.lock = threading.Lock()

    def allow(self, ip, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            self.attempts = [(at, client) for at, client in self.attempts if at > now - 60]
            if len(self.attempts) >= 60 or sum(client == ip for _, client in self.attempts) >= 8:
                return False
            self.attempts.append((now, ip))
            return True
