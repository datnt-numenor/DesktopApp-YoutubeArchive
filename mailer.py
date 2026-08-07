import asyncio
import logging
import smtplib
from email.message import EmailMessage

from config import settings


logger = logging.getLogger(__name__)


def _smtp_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_from_email)


async def send_email(to_email: str, subject: str, body: str) -> bool:
    if not _smtp_configured():
        logger.warning("SMTP is not configured. Email to %s with subject %r:\n%s", to_email, subject, body)
        return False

    message = EmailMessage()
    message["From"] = settings.smtp_from_email
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)

    def send() -> None:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_username and settings.smtp_password:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)

    await asyncio.to_thread(send)
    return True
