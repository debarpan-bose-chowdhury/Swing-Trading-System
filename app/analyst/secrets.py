"""Secrets for the Stock Analyst, read only from environment variables (never from files in the repo or data folders).

On the host they live in an env-file outside the mounted folders and reach the container via `docker run --env-file`.
Values are never logged or included in error messages; only the variable names are.
"""

import os

BROKER = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_MPIN", "ANGEL_TOTP_SECRET")
SMTP = ("SMTP_USER", "SMTP_PASSWORD")


def _unset(value: str | None) -> bool:
    return value is None or not value.strip() or value.strip().startswith("<")


def load(names: tuple[str, ...] = BROKER) -> dict[str, str]:
    """Return {name: value}; raise ValueError listing every variable that is missing or still a placeholder."""
    missing = [n for n in names if _unset(os.environ.get(n))]
    if missing:
        raise ValueError(f"missing or placeholder secrets: {', '.join(missing)} (see .env.example)")
    return {n: os.environ[n].strip() for n in names}
