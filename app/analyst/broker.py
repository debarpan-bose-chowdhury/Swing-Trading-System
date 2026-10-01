"""Read-only Angel One SmartAPI client: login, holdings, positions, tradebook, funds. No order or GTT call exists here.

A thin client over `requests` instead of the official SDK, which looks up the public IP over the network at import time,
creates a `logs/` folder in the working directory and writes request headers (API key, bearer token) to its log on errors.
Endpoints and field names come from the SDK source and SmartAPI documentation excerpts; `probe --check-broker`
confirms them against the real account. Secrets, tokens and TOTP codes are never logged; only routes and error codes are.
"""

import base64
import hmac
import json
import logging
import random
import struct
import time

import requests

ROOT = "https://apiconnect.angelone.in"
ROUTES = {
    "login": ("POST", "/rest/auth/angelbroking/user/v1/loginByPassword"),
    "logout": ("POST", "/rest/secure/angelbroking/user/v1/logout"),
    "holdings": ("GET", "/rest/secure/angelbroking/portfolio/v1/getHolding"),
    "positions": ("GET", "/rest/secure/angelbroking/order/v1/getPosition"),
    "tradebook": ("GET", "/rest/secure/angelbroking/order/v1/getTradeBook"),
    "funds": ("GET", "/rest/secure/angelbroking/user/v1/getRMS"),
}
# Fields the Ledger relies on (SmartAPI documentation excerpts; UNVERIFIED until the probe has been run).
EXPECTED = {
    "holdings": ["tradingsymbol", "quantity", "t1quantity", "averageprice", "ltp", "product"],
    "positions": ["tradingsymbol", "producttype", "netqty"],
    "tradebook": ["tradingsymbol", "producttype", "exchange", "transactiontype", "fillprice", "fillsize", "fillid", "filltime", "orderid"],
    "funds": [],
}
BROKER_SECRETS = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_MPIN", "ANGEL_TOTP_SECRET")
INVALID_TOTP = {"AB1050"}
TOKEN_ERRORS = {"AG8001", "AG8002", "AB8051"}
RETRYABLE = {"AB1004", "AB1021"}
RATE_TEXT = "exceeding access rate"


class BrokerError(Exception):
    def __init__(self, code: str, route: str):
        super().__init__(f"{route}: {code}")  # never the response body or headers
        self.code, self.route = code, route


class LoginFailed(Exception):
    pass


def totp(secret: str, now: float | None = None, digits: int = 6, step: int = 30) -> str:
    """RFC 6238 time-based one-time password (HMAC-SHA1), from a base32 secret."""
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    digest = hmac.new(key, struct.pack(">Q", int((time.time() if now is None else now) // step)), "sha1").digest()  # NOSONAR: HMAC-SHA1 is what RFC 6238 and authenticator apps require
    offset = digest[-1] & 0x0F
    return str((struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 10**digits).zfill(digits)


class Broker:
    def __init__(self, cfg: dict, secrets: dict, log: logging.Logger, session=None, sleep=time.sleep, clock=time.monotonic):
        self.c, self.secrets, self.log = cfg["broker"], secrets, log
        self.session, self.sleep, self.clock = session or requests.Session(), sleep, clock
        self.jwt: str | None = None
        self.last_call: float | None = None
        self.relogged = False

    # --- transport -----------------------------------------------------------------------------------------
    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json", "Accept": "application/json", "X-UserType": "USER", "X-SourceID": "WEB",
            "X-ClientLocalIP": self.c["clientLocalIp"], "X-ClientPublicIP": self.c["clientPublicIp"],
            "X-MACAddress": self.c["clientMacAddress"], "X-PrivateKey": self.secrets["ANGEL_API_KEY"],
        }
        if self.jwt:
            h["Authorization"] = f"Bearer {self.jwt}"
        return h

    def _once(self, route: str, body: dict | None):
        if self.last_call is not None:
            self.sleep(max(0.0, self.c["minCallGapSeconds"] - (self.clock() - self.last_call)))
        method, path = ROUTES[route]
        try:
            r = self.session.request(method, ROOT + path, headers=self._headers(), timeout=self.c["timeoutSeconds"],
                                     data=json.dumps(body) if body is not None else None)
        finally:
            self.last_call = self.clock()
        try:
            data = r.json()
        except ValueError:
            raise BrokerError("RATE_LIMITED" if RATE_TEXT in r.text.lower() else f"HTTP_{r.status_code}", route) from None
        if not isinstance(data, dict) or not data.get("status"):
            raise BrokerError(str((data or {}).get("errorcode") or f"HTTP_{r.status_code}"), route)
        return data.get("data")

    def _call(self, route: str, body: dict | None = None):
        """One request with retries (timeouts, rate limits, AB1004/AB1021) and a single re-login on token errors."""
        attempt = 0
        while True:
            try:
                return self._once(route, body)
            except BrokerError as e:
                if e.code in TOKEN_ERRORS and not self.relogged and route != "login":
                    self.relogged = True
                    self.log.warning("%s: %r; logging in again once", route, e.code)
                    self.login()
                    continue
                if e.code not in RETRYABLE | {"RATE_LIMITED"} or attempt >= self.c["maxRetries"]:
                    raise
                self.log.warning("%s: %r; retry %d", route, e.code, attempt + 1)
            except requests.RequestException as e:
                if attempt >= self.c["maxRetries"]:
                    raise BrokerError(type(e).__name__, route) from None
                self.log.warning("%s: %s; retry %d", route, type(e).__name__, attempt + 1)
            backoff = self.c["backoffSeconds"]
            self.sleep(backoff[min(attempt, len(backoff) - 1)] + random.SystemRandom().random())
            attempt += 1

    # --- session ---------------------------------------------------------------------------------------------
    def login(self) -> None:
        """loginByPassword with client code, MPIN and a fresh TOTP; an invalid TOTP is retried after a pause."""
        for attempt in range(self.c["loginAttempts"]):
            body = {"clientcode": self.secrets["ANGEL_CLIENT_CODE"], "password": self.secrets["ANGEL_MPIN"],
                    "totp": totp(self.secrets["ANGEL_TOTP_SECRET"])}
            try:
                data = self._call("login", body)
            except BrokerError as e:
                if e.code in INVALID_TOTP and attempt + 1 < self.c["loginAttempts"]:
                    self.log.warning("login: invalid TOTP; waiting %ds for a fresh code", self.c["loginRetryGapSeconds"])
                    self.sleep(self.c["loginRetryGapSeconds"])
                    continue
                raise LoginFailed(f"broker login failed: {e.code}") from None
            if not data or not data.get("jwtToken"):
                raise LoginFailed("broker login returned no token")
            self.jwt = data["jwtToken"]  # kept in memory only
            return
        raise LoginFailed("broker login failed: invalid TOTP")

    def logout(self) -> None:
        if not self.jwt:
            return
        try:
            self._once("logout", {"clientcode": self.secrets["ANGEL_CLIENT_CODE"]})
        except Exception:  # best effort; the session ends at midnight anyway
            self.log.info("logout skipped")

    # --- read endpoints --------------------------------------------------------------------------------------
    def _rows(self, route: str) -> list[dict]:
        data = self._call(route)
        if isinstance(data, dict) and isinstance(data.get("holdings"), list):  # getAllHolding-style wrapper
            data = data["holdings"]
        return list(data or [])

    def holdings(self) -> list[dict]:
        return self._rows("holdings")

    def positions(self) -> list[dict]:
        return self._rows("positions")

    def tradebook(self) -> list[dict]:
        return self._rows("tradebook")

    def funds(self) -> dict:
        return self._call("funds") or {}
