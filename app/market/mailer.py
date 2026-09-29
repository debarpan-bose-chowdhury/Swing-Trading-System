"""Digest email over SMTP (STARTTLS); credentials only from SMTP_USER / SMTP_PASSWORD."""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage


def send(cfg: dict, subject: str, body: str, log: logging.Logger) -> bool:
    """Best-effort: a failed or unconfigured send is logged and never fails the run."""
    mail = cfg["mail"]
    recipients = [r for r in mail["recipients"] if not r.startswith("<")]
    if mail["smtpHost"].startswith("<") or mail["sender"].startswith("<") or not recipients:
        log.warning("Mail not configured; digest not sent:\n%s\n%s", subject, body)
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, mail["sender"], ", ".join(recipients)
    msg.set_content(body)
    try:
        with smtplib.SMTP(mail["smtpHost"], mail["smtpPort"], timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if os.environ.get("SMTP_USER"):
                smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            smtp.send_message(msg)
        return True
    except Exception:
        log.exception("Digest email failed")
        return False
