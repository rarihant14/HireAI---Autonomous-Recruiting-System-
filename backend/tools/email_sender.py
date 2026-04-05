"""
backend/tools/email_sender.py — Outgoing Email Sender
Handles all SMTP operations: plain text, HTML, and thread-aware replies.

Separated from engagement_agent.py so:
  - SMTP logic can be tested independently
  - Email templates live in one place
  - Retry / error handling doesn't clutter the LangGraph agent
"""

import smtplib
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from backend.config import cfg
from backend.utils import log, retry


# ── Result type ───────────────────────────────────────────────────────────────

class SendResult:
    __slots__ = ("success", "message_id", "error")

    def __init__(self, success: bool, message_id: str = "", error: str = ""):
        self.success    = success
        self.message_id = message_id
        self.error      = error

    def to_dict(self) -> dict:
        return {"success": self.success, "message_id": self.message_id, "error": self.error}


# ── Core send function ────────────────────────────────────────────────────────

@retry(times=2, delay=2.0, exceptions=(smtplib.SMTPException, OSError))
def send_email(
    to_addr:     str,
    subject:     str,
    body_plain:  str,
    body_html:   Optional[str] = None,
    reply_to_id: Optional[str] = None,   # Message-ID of the email being replied to
    cc:          Optional[list] = None,
) -> SendResult:
    """
    Send an email via SMTP (TLS).

    Args:
        to_addr      — recipient email address
        subject      — email subject line
        body_plain   — plain-text body (always required)
        body_html    — optional HTML body (sent as multipart/alternative)
        reply_to_id  — if set, adds In-Reply-To / References headers for threading
        cc           — optional list of CC addresses

    Returns:
        SendResult with success flag, generated Message-ID, and any error string.
    """
    if not cfg.SMTP_USER or not cfg.SMTP_PASSWORD:
        log("EmailSender", "SMTP credentials not configured — cannot send", "WARN")
        return SendResult(False, error="SMTP credentials not configured")

    # Build message
    msg = MIMEMultipart("alternative") if body_html else MIMEMultipart()
    generated_id   = f"<{uuid.uuid4()}@hireai.local>"
    msg["Message-ID"] = generated_id
    msg["From"]       = cfg.SMTP_USER
    msg["To"]         = to_addr
    msg["Subject"]    = subject

    if cc:
        msg["Cc"] = ", ".join(cc)

    # Threading headers — keeps all emails in one Gmail thread
    if reply_to_id:
        clean_id       = reply_to_id.strip("<>")
        msg["In-Reply-To"] = f"<{clean_id}>"
        msg["References"]  = f"<{clean_id}>"

    # Attach body parts
    msg.attach(MIMEText(body_plain, "plain", "utf-8"))
    if body_html:
        msg.attach(MIMEText(body_html, "html", "utf-8"))

    # Send
    try:
        with smtplib.SMTP(cfg.SMTP_HOST, cfg.SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(cfg.SMTP_USER, cfg.SMTP_PASSWORD)
            recipients = [to_addr] + (cc or [])
            server.sendmail(cfg.SMTP_USER, recipients, msg.as_string())

        log("EmailSender", f"Sent to {to_addr} | subject: {subject[:60]}")
        return SendResult(True, message_id=generated_id)

    except smtplib.SMTPAuthenticationError as e:
        log("EmailSender", f"Auth error: {e}", "ERROR")
        return SendResult(False, error=f"Auth failed: {e}")
    except smtplib.SMTPRecipientsRefused as e:
        log("EmailSender", f"Recipient refused: {to_addr} — {e}", "ERROR")
        return SendResult(False, error=f"Recipient refused: {e}")
    except Exception as e:
        log("EmailSender", f"Unexpected error: {e}", "ERROR")
        return SendResult(False, error=str(e))


# ── Template helpers ──────────────────────────────────────────────────────────

def build_initial_email(name: str, job_role: str, question: str) -> tuple[str, str]:
    """
    Build the Round 1 outreach email body (plain text + HTML).
    Returns (plain_text, html).
    """
    plain = (
        f"Hi {name},\n\n"
        f"Thanks for applying for the {job_role} role. "
        "We reviewed your application and would love to learn more about you.\n\n"
        f"{question}\n\n"
        "Looking forward to hearing from you.\n\n"
        "The Hiring Team"
    )
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;color:#222">
      <p>Hi <strong>{name}</strong>,</p>
      <p>Thanks for applying for the <strong>{job_role}</strong> role.
         We reviewed your application and would love to learn more about you.</p>
      <blockquote style="border-left:3px solid #f5a623;padding-left:16px;color:#444">
        {question}
      </blockquote>
      <p>Looking forward to hearing from you.</p>
      <p style="color:#888;font-size:13px">— The Hiring Team</p>
    </div>"""
    return plain, html


def build_rejection_email(name: str, job_role: str) -> tuple[str, str]:
    """Build a respectful rejection email."""
    plain = (
        f"Hi {name},\n\n"
        f"Thank you for your interest in the {job_role} position. "
        "After careful review we will not be moving forward with your application at this time. "
        "We wish you the very best in your job search.\n\n"
        "The Hiring Team"
    )
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;color:#222">
      <p>Hi <strong>{name}</strong>,</p>
      <p>Thank you for your interest in the <strong>{job_role}</strong> position.
         After careful review we will not be moving forward with your application at this time.</p>
      <p>We wish you the very best in your job search.</p>
      <p style="color:#888;font-size:13px">— The Hiring Team</p>
    </div>"""
    return plain, html
