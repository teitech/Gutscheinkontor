"""E-Mail-Versand über einen frei konfigurierbaren SMTP-Server."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from .config import get_settings

logger = logging.getLogger("gutscheinkontor.mail")

SMTP_TIMEOUT = 15


def is_enabled() -> bool:
    settings = get_settings()
    return bool(settings.smtp_host and settings.smtp_from)


def send_mail(to: str, subject: str, body: str) -> None:
    """Verschickt eine Text-Mail. Fehler werden protokolliert, aber nicht weitergegeben.

    Gedacht für Hintergrundaufgaben: Die Antwort an den Browser darf nie davon abhängen,
    ob der Versand klappt (sonst ließe sich ablesen, ob eine Adresse existiert).
    """
    settings = get_settings()
    if not is_enabled():
        logger.warning("E-Mail-Versand ist nicht eingerichtet, Nachricht wird verworfen.")
        return

    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    domain = parseaddr(settings.smtp_from)[1].rpartition("@")[2] or None
    message["Message-ID"] = make_msgid(domain=domain)
    message.set_content(body)

    context = ssl.create_default_context()
    try:
        if settings.smtp_security == "ssl":
            server = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT, context=context
            )
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT)
        with server:
            server.ehlo()
            if settings.smtp_security == "starttls":
                server.starttls(context=context)
                server.ehlo()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)
    except Exception as exc:  # noqa: BLE001 - bewusst breit, siehe Docstring
        # Keine Adresse und kein Nachrichtentext im Log (Datenschutz).
        logger.error("E-Mail-Versand fehlgeschlagen (%s).", type(exc).__name__)
