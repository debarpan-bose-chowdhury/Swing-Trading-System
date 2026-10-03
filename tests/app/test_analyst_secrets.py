"""Secrets helper: reads env vars, rejects missing/placeholder values, never leaks values."""

import re
import unittest
from pathlib import Path
from unittest.mock import patch

from app.analyst import secrets

GOOD = {"ANGEL_API_KEY": "key123", "ANGEL_CLIENT_CODE": "A1", "ANGEL_MPIN": "9876", "ANGEL_TOTP_SECRET": "JBSWY3DPEHPK3PXP"}


class SecretsTests(unittest.TestCase):
    def test_loads_and_strips(self):
        with patch.dict("os.environ", {**GOOD, "ANGEL_MPIN": " 9876 "}, clear=True):
            self.assertEqual(secrets.load()["ANGEL_MPIN"], "9876")

    def test_missing_and_placeholder_are_reported_by_name_only(self):
        env = {**GOOD, "ANGEL_API_KEY": "<set at deployment>", "ANGEL_MPIN": "  "}
        del env["ANGEL_TOTP_SECRET"]
        with patch.dict("os.environ", env, clear=True), self.assertRaises(ValueError) as cm:
            secrets.load()
        msg = str(cm.exception)
        for name in ("ANGEL_API_KEY", "ANGEL_MPIN", "ANGEL_TOTP_SECRET"):
            self.assertIn(name, msg)
        self.assertNotIn("ANGEL_CLIENT_CODE", msg)

    def test_values_never_appear_in_errors(self):
        with patch.dict("os.environ", {"SMTP_USER": "me@example.com"}, clear=True), self.assertRaises(ValueError) as cm:
            secrets.load(secrets.SMTP)
        self.assertNotIn("me@example.com", str(cm.exception))

    def test_env_example_lists_every_secret_as_a_placeholder_and_is_ignored_when_real(self):
        text = Path(".env.example").read_text(encoding="utf-8")
        for name in secrets.BROKER + secrets.SMTP:
            self.assertRegex(text, rf"(?m)^{name}=<set at deployment>$")
        for f in (".gitignore", ".dockerignore"):
            self.assertIsNotNone(re.search(r"(?m)^\*\.env$", Path(f).read_text(encoding="utf-8")), f)


if __name__ == "__main__":
    unittest.main()
