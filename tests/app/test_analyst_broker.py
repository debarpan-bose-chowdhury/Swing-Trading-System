"""Broker client and probe against a fake HTTP session: TOTP vectors, retries, token errors, no secret leaks."""

import contextlib
import copy
import io
import json
import logging
import unittest
from unittest.mock import patch

import requests

from app.analyst import broker, common, probe
from app.analyst.broker import Broker, BrokerError, LoginFailed

CFG = common.load_config()
SECRETS = {"ANGEL_API_KEY": "KEY-SECRET", "ANGEL_CLIENT_CODE": "C123", "ANGEL_MPIN": "9876", "ANGEL_TOTP_SECRET": "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"}


class Resp:
    def __init__(self, body=None, text="", status=200):
        self.body, self.text, self.status_code = body, text or json.dumps(body), status

    def json(self):
        if self.body is None:
            raise ValueError("not json")
        return self.body


def ok(data=None):
    return Resp({"status": True, "message": "SUCCESS", "errorcode": "", "data": data})


def err(code):
    return Resp({"status": False, "message": "secret-looking message", "errorcode": code, "data": None})


class Session:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def request(self, method, url, headers=None, timeout=None, data=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "data": data, "timeout": timeout})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class Clock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def __call__(self):
        return self.t


def make(script):
    clock, session = Clock(), Session(script)
    log = logging.getLogger("test.broker")
    return Broker(copy.deepcopy(CFG), SECRETS, log, session=session, sleep=clock.sleep, clock=clock), session, clock


LOGIN = ok({"jwtToken": "JWT-SECRET", "refreshToken": "R", "feedToken": "F"})


class TotpTests(unittest.TestCase):
    def test_rfc6238_sha1_vectors(self):
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 of "12345678901234567890"
        for t, code in ((59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")):
            self.assertEqual(broker.totp(secret, now=t), code)

    def test_lowercase_spaces_and_missing_padding_are_accepted(self):
        self.assertEqual(broker.totp("gezd gnbv gy3t qojq gezd gnbv gy3t qojq", now=59), "287082")


class LoginTests(unittest.TestCase):
    def test_login_sends_credentials_and_keeps_the_token_in_memory(self):
        b, s, _ = make([LOGIN])
        b.login()
        call = s.calls[0]
        body = json.loads(call["data"])
        self.assertEqual((body["clientcode"], body["password"]), ("C123", "9876"))
        self.assertRegex(body["totp"], r"^\d{6}$")
        self.assertEqual(call["headers"]["X-PrivateKey"], "KEY-SECRET")
        self.assertNotIn("Authorization", call["headers"])
        self.assertEqual(b.jwt, "JWT-SECRET")

    def test_invalid_totp_waits_for_a_fresh_code_then_gives_up_after_the_attempts(self):
        b, s, clock = make([err("AB1050"), err("AB1050"), err("AB1050")])
        with self.assertRaisesRegex(LoginFailed, "AB1050"):
            b.login()
        self.assertEqual(len(s.calls), 3)
        self.assertEqual(clock.sleeps.count(60), 2)

    def test_invalid_totp_then_success(self):
        b, s, _ = make([err("AB1050"), LOGIN])
        b.login()
        self.assertEqual(b.jwt, "JWT-SECRET")

    def test_other_login_errors_fail_at_once_without_leaking_the_message(self):
        b, _, _ = make([err("AB1000")])
        with self.assertRaises(LoginFailed) as cm:
            b.login()
        self.assertNotIn("secret-looking", str(cm.exception))

    def test_login_without_a_token_fails(self):
        b, _, _ = make([ok({})])
        with self.assertRaises(LoginFailed):
            b.login()


class CallTests(unittest.TestCase):
    def logged_in(self, script):
        b, s, clock = make([LOGIN, *script])
        b.login()
        return b, s, clock

    def test_reads_use_get_with_the_bearer_token_and_unwrap_rows(self):
        b, s, _ = self.logged_in([ok([{"a": 1}]), ok({"holdings": [{"b": 2}], "totalholding": {}}), ok(None), ok({"net": "1"})])
        self.assertEqual(b.tradebook(), [{"a": 1}])
        self.assertEqual(b.holdings(), [{"b": 2}])
        self.assertEqual(b.positions(), [])
        self.assertEqual(b.funds(), {"net": "1"})
        paths = [c["url"].rsplit("/", 1)[1] for c in s.calls[1:]]
        self.assertEqual(paths, ["getTradeBook", "getHolding", "getPosition", "getRMS"])
        self.assertTrue(all(c["method"] == "GET" and c["headers"]["Authorization"] == "Bearer JWT-SECRET" for c in s.calls[1:]))
        self.assertTrue(all(c["timeout"] == 15 for c in s.calls))

    def test_calls_are_at_least_a_second_apart(self):
        b, _, clock = self.logged_in([ok([]), ok([])])
        b.holdings()
        b.positions()
        self.assertGreaterEqual(clock.t, 2.0)

    def test_retryable_errors_back_off_two_four_eight_then_fail(self):
        b, s, clock = self.logged_in([err("AB1021")] * 4)
        with self.assertRaises(BrokerError):
            b.holdings()
        self.assertEqual(len(s.calls), 1 + 4)
        waits = [x for x in clock.sleeps if x >= 2]
        self.assertEqual([int(w) for w in waits[:3]], [2, 4, 8])

    def test_plain_text_rate_limit_and_timeouts_are_retried(self):
        text = Resp(None, text="Access denied because of exceeding access rate")
        b, _, _ = self.logged_in([text, requests.Timeout(), ok([{"x": 1}])])
        self.assertEqual(b.holdings(), [{"x": 1}])

    def test_token_error_logs_in_again_once_then_fails_on_the_second(self):
        b, s, _ = self.logged_in([err("AG8002"), LOGIN, ok([{"x": 1}])])
        self.assertEqual(b.holdings(), [{"x": 1}])
        self.assertEqual(len(s.calls), 4)
        b, _, _ = self.logged_in([err("AG8001"), LOGIN, err("AB8051")])
        with self.assertRaises(BrokerError):
            b.holdings()

    def test_non_retryable_error_raises_immediately_with_code_only(self):
        b, s, _ = self.logged_in([err("AB9999")])
        with self.assertRaises(BrokerError) as cm:
            b.holdings()
        self.assertEqual(cm.exception.code, "AB9999")
        self.assertNotIn("secret-looking", str(cm.exception))
        self.assertEqual(len(s.calls), 2)

    def test_no_secret_reaches_the_logs(self):
        b, _, _ = make([LOGIN, err("AB1021"), err("AB1021"), err("AB1021"), err("AB1021")])
        with self.assertLogs("test.broker", level="INFO") as logs, contextlib.suppress(BrokerError):
            b.login()
            b.holdings()
        text = "\n".join(logs.output)
        for secret in ("KEY-SECRET", "JWT-SECRET", "9876", "GEZDGNBV"):
            self.assertNotIn(secret, text)

    def test_the_client_exposes_no_order_or_gtt_method(self):
        names = [n for n in dir(Broker) if not n.startswith("_")]
        self.assertFalse([n for n in names if "order" in n.lower() or "gtt" in n.lower() or "place" in n.lower()])
        self.assertFalse([r for r in broker.ROUTES if "order/v1/place" in broker.ROUTES[r][1] or "gtt" in broker.ROUTES[r][1]])


class ProbeTests(unittest.TestCase):
    def run_probe(self, script):
        b, _, _ = make([LOGIN, *script])
        b.login()
        lines = []
        problems = probe.probe(b, out=lines.append)
        return problems, lines

    def good(self):
        h = {f: "1" for f in broker.EXPECTED["holdings"]}
        p = {f: "1" for f in broker.EXPECTED["positions"]}
        t = {f: "1" for f in broker.EXPECTED["tradebook"]}
        return [ok([h]), ok([p]), ok([t]), ok({"net": "1.0"})]

    def test_lists_field_names_and_types_but_never_values(self):
        problems, lines = self.run_probe(self.good())
        text = "\n".join(lines)
        self.assertEqual(problems, 0)
        self.assertIn("holdings: 1 row(s)", text)
        self.assertIn("  t1quantity: str", text)
        self.assertIn("  net: str", text)

    def test_reports_missing_fields_failures_and_empty_endpoints(self):
        h = {f: "1" for f in broker.EXPECTED["holdings"] if f != "t1quantity"}
        problems, lines = self.run_probe([ok([h]), err("AB9999"), ok([]), ok({})])
        text = "\n".join(lines)
        self.assertEqual(problems, 2)
        self.assertIn("MISSING expected fields: t1quantity", text)
        self.assertIn("positions: FAILED AB9999", text)
        self.assertIn("cannot be confirmed now", text)

    def test_main_requires_the_flag_and_reports_missing_secrets(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            probe.main([])
        with patch.dict("os.environ", {}, clear=True), contextlib.redirect_stderr(io.StringIO()) as err_out:
            self.assertEqual(probe.main(["--check-broker"]), 1)
        self.assertIn("ANGEL_API_KEY", err_out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(probe.main(["--check"]), 0)
        self.assertIn("probe: check ok", out.getvalue())


if __name__ == "__main__":
    unittest.main()
