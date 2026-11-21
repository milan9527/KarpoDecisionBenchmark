"""Credential guessing, malformed cookies and session expiry must fail closed."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo/backend"))
from auth import COOKIE_SECONDS, LoginLimiter, hash_password, issue_session, read_session, verify_credentials, viewer_ip


class AuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "test-only-password-不含真实凭据"
        cls.account = {"username": "demo", "password_hash": hash_password(cls.password)}
        cls.key = "test-only-cookie-key"
        cls.session = "a" * 32

    def test_password_hash_is_salted_and_checks_both_credentials(self):
        self.assertNotEqual(self.account["password_hash"], hash_password(self.password))
        self.assertNotIn(self.password, self.account["password_hash"])
        self.assertTrue(verify_credentials("demo", self.password, self.account))
        self.assertFalse(verify_credentials("demo", self.password + "x", self.account))
        self.assertFalse(verify_credentials("missing", self.password, self.account))

    def test_unknown_username_still_derives_password(self):
        with patch("auth.hashlib.pbkdf2_hmac", wraps=hashlib.pbkdf2_hmac) as derive:
            self.assertFalse(verify_credentials("missing", self.password, self.account))
            derive.assert_called_once()

    def test_invalid_credential_types_and_lengths(self):
        for username, password in [
            (None, self.password), ([], self.password), ("demo", {}),
            ("demo", ""), ("demo", "x" * 1025), ("x" * 65, self.password), ("demo", "\ud800"),
        ]:
            with self.subTest(username=username):
                self.assertFalse(verify_credentials(username, password, self.account))

    def test_session_expires_at_eight_hours(self):
        cookie = issue_session(self.session, self.key, "demo", now=1000)
        self.assertEqual(read_session(cookie, self.key, "demo", now=1001), self.session)
        with self.assertRaises(ValueError):
            read_session(cookie, self.key, "demo", now=1000 + COOKIE_SECONDS)

    def test_cookie_tampering_key_rotation_and_account_change(self):
        cookie = issue_session(self.session, self.key, "demo", now=1000)
        for candidate, key, username in [
            (cookie[:-1] + ("0" if cookie[-1] != "0" else "1"), self.key, "demo"),
            (cookie, "rotated-key", "demo"), (cookie, self.key, "another-account"),
            (None, self.key, "demo"), ("garbage", self.key, "demo"),
            ("." * 5000, self.key, "demo"), ("a.非ASCII", self.key, "demo"),
        ]:
            with self.subTest(username=username):
                with self.assertRaises(ValueError):
                    read_session(candidate, key, username, now=1001)

    def test_signed_legacy_and_malformed_claims_are_rejected(self):
        claims = [
            {"session": self.session, "exp": 9999},  # old access-code cookie
            {"v": 1, "username": "demo", "session": [], "exp": 9999},
            {"v": 1, "username": "demo", "session": self.session, "exp": "9999"},
            [],
        ]
        for value in claims:
            body = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
            signature = hmac.new(self.key.encode(), body.encode(), hashlib.sha256).hexdigest()
            with self.assertRaises(ValueError):
                read_session(body + "." + signature, self.key, "demo", now=1001)

    def test_limiter_window_and_parallel_attempts(self):
        limiter = LoginLimiter()
        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(lambda _: limiter.allow("same-client", now=1000), range(20)))
        self.assertEqual(sum(results), 8)
        self.assertFalse(limiter.allow("same-client", now=1059))
        self.assertTrue(limiter.allow("same-client", now=1060))
        self.assertTrue(limiter.allow("other-client", now=1060))

    def test_limiter_global_budget_is_bounded(self):
        limiter = LoginLimiter()
        for i in range(60):
            self.assertTrue(limiter.allow(str(i), now=1000))
        self.assertFalse(limiter.allow("another", now=1000))
        self.assertTrue(limiter.allow("another", now=1060))
        self.assertEqual(len(limiter.attempts), 1)

    def test_forwarded_header_cannot_override_appended_viewer(self):
        for header in ("viewer, cloudfront", "spoofed, viewer, cloudfront", "a, b, viewer, cloudfront"):
            self.assertEqual(viewer_ip(header, "alb"), "viewer")
        self.assertEqual(viewer_ip("spoofed", "direct-client"), "direct-client")


if __name__ == "__main__":
    unittest.main()
