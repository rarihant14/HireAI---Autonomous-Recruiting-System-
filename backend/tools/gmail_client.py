"""
backend/tools/gmail_client.py — Gmail API Client
Uses the official Google Gmail API with OAuth2 (credentials.json).

WHY THIS IS THE PRIMARY EMAIL TRANSPORT:
  - No "App Password" needed
  - reCAPTCHA / suspicious-login blocks don't apply
  - Thread IDs are native to Gmail API (no In-Reply-To header juggling)
  - Reading unread messages is a single API call
  - Sending preserves Gmail threads perfectly

FIRST RUN:
  The first time you run the system, a browser window will open asking you
  to authorise the app with your Google account.
  After that, a token.json is saved and reused automatically.

SCOPES USED:
  gmail.send   — send emails on your behalf
  gmail.modify — read + mark messages as read (needed for inbox polling)
"""

import base64
import re
from pathlib import Path
from typing import Optional
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from backend.tools.email_sender import send_email as send_smtp_email
from backend.utils import log, retry

# ── OAuth2 scopes ─────────────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

# ── File paths ────────────────────────────────────────────────────────────────
_PROJECT_ROOT    = Path(__file__).parent.parent.parent
CREDENTIALS_FILE = _PROJECT_ROOT / "credentials.json"
TOKEN_FILE       = _PROJECT_ROOT / "token.json"


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_gmail_service():
    """
    Build and return an authorised Gmail API service object.

    Flow:
      1. If token.json exists and is valid → use it directly.
      2. If token.json is expired → refresh using the refresh_token.
      3. If no token.json → open browser for user consent, save token.json.
    """
    creds = None

    # Load existing token
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    # Refresh or re-authenticate
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log("GmailClient", "Refreshing expired token…")
            creds.refresh(Request())
        else:
            log("GmailClient", "No valid token found — opening browser for OAuth2 consent…")
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"credentials.json not found at {CREDENTIALS_FILE}. "
                    "Place your Google OAuth2 credentials file in the project root."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            # port=0 picks a random free port, avoiding conflicts
            creds = flow.run_local_server(port=0)

        # Save token for next run
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        log("GmailClient", f"Token saved to {TOKEN_FILE}")

    return build("gmail", "v1", credentials=creds)


# ── Send ──────────────────────────────────────────────────────────────────────

def _encode_message(msg: MIMEMultipart) -> dict:
    """Encode a MIMEMultipart message to the Gmail API raw format."""
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return {"raw": raw}


def _encode_thread_message(msg: MIMEMultipart, thread_id: str) -> dict:
    """Encode message and attach it to an existing Gmail thread."""
    payload = _encode_message(msg)
    payload["threadId"] = thread_id
    return payload


def _smtp_fallback(
    to_addr: str,
    subject: str,
    body_plain: str,
    body_html: Optional[str] = None,
) -> dict:
    """Fallback transport when Gmail API is unavailable."""
    result = send_smtp_email(
        to_addr=to_addr,
        subject=subject,
        body_plain=body_plain,
        body_html=body_html,
    )
    if result.success:
        log("GmailClient", f"SMTP fallback sent to {to_addr}")
        return {
            "success": True,
            "message_id": result.message_id,
            "thread_id": "",
            "error": "",
        }

    log("GmailClient", f"SMTP fallback failed for {to_addr}: {result.error}", "ERROR")
    return {
        "success": False,
        "message_id": "",
        "thread_id": "",
        "error": result.error,
    }


@retry(times=2, delay=2.0, exceptions=(HttpError,))
def send_email(
    to_addr:     str,
    subject:     str,
    body_plain:  str,
    body_html:   Optional[str] = None,
    thread_id:   Optional[str] = None,   # Gmail threadId (not Message-ID)
    cc:          Optional[list] = None,
) -> dict:
    """
    Send an email via Gmail API, with SMTP as a fallback transport.

    Args:
        to_addr    — recipient email
        subject    — subject line
        body_plain — plain-text body
        body_html  — optional HTML body
        thread_id  — Gmail threadId to reply within (keeps conversation grouped)
        cc         — optional CC list

    Returns:
        {"success": bool, "message_id": str, "thread_id": str, "error": str}
    """
    try:
        service = get_gmail_service()

        msg = MIMEMultipart("alternative") if body_html else MIMEMultipart()
        msg["To"]      = to_addr
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = ", ".join(cc)

        msg.attach(MIMEText(body_plain, "plain", "utf-8"))
        if body_html:
            msg.attach(MIMEText(body_html, "html", "utf-8"))

        # Attach to thread if provided
        payload = _encode_thread_message(msg, thread_id) if thread_id else _encode_message(msg)

        result = service.users().messages().send(
            userId="me", body=payload
        ).execute()

        log("GmailClient", f"Sent to {to_addr} | thread={result.get('threadId','?')}")
        return {
            "success":    True,
            "message_id": result.get("id", ""),
            "thread_id":  result.get("threadId", ""),
            "error":      "",
        }

    except HttpError as e:
        log("GmailClient", f"HttpError sending to {to_addr}: {e}", "ERROR")
        fallback = _smtp_fallback(to_addr, subject, body_plain, body_html)
        if fallback["success"]:
            fallback["error"] = f"Gmail API failed, SMTP fallback used: {e}"
        return fallback
    except Exception as e:
        log("GmailClient", f"Error sending to {to_addr}: {e}", "ERROR")
        fallback = _smtp_fallback(to_addr, subject, body_plain, body_html)
        if fallback["success"]:
            fallback["error"] = f"Gmail API failed, SMTP fallback used: {e}"
        return fallback


# ── Read ──────────────────────────────────────────────────────────────────────

def _decode_body(payload: dict) -> str:
    """Recursively extract plain-text body from a Gmail message payload."""
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime_type == "text/plain" and body_data:
        return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")

    # Recurse into multipart parts
    for part in payload.get("parts", []):
        result = _decode_body(part)
        if result:
            # Strip quoted reply lines (">" prefix)
            lines = result.splitlines()
            clean = [l for l in lines if not l.strip().startswith(">")]
            return "\n".join(clean).strip()

    # Fallback: strip HTML tags if no plain text found
    if mime_type == "text/html" and body_data:
        html = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
        return re.sub(r"<[^>]+>", " ", html).strip()

    return ""


def _parse_header(headers: list, name: str) -> str:
    """Extract a header value by name from Gmail message headers list."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


@retry(times=2, delay=1.0, exceptions=(HttpError,))
def fetch_unread_messages(max_results: int = 50) -> list[dict]:
    """
    Fetch all unread messages from INBOX via Gmail API.
    Marks them as read after fetching.

    Returns list of:
    {
      "gmail_id":   str,   # Gmail message ID
      "thread_id":  str,   # Gmail thread ID
      "from_email": str,
      "from_name":  str,
      "subject":    str,
      "body":       str,
      "date":       str,
    }
    """
    results = []
    try:
        service = get_gmail_service()

        # Search for unread messages in INBOX
        response = service.users().messages().list(
            userId="me",
            q="is:unread in:inbox",
            maxResults=max_results,
        ).execute()

        messages = response.get("messages", [])
        if not messages:
            log("GmailClient", "No unread messages found")
            return []

        log("GmailClient", f"Found {len(messages)} unread message(s)")

        for msg_ref in messages:
            msg_id = msg_ref["id"]
            try:
                # Fetch full message
                msg = service.users().messages().get(
                    userId="me", id=msg_id, format="full"
                ).execute()

                headers   = msg.get("payload", {}).get("headers", [])
                from_raw  = _parse_header(headers, "From")
                subject   = _parse_header(headers, "Subject")
                date      = _parse_header(headers, "Date")
                body      = _decode_body(msg.get("payload", {}))

                # Extract email address from "Name <email>" format
                email_match = re.search(r"[\w.+\-]+@[\w\-]+\.[a-zA-Z]+", from_raw)
                from_email  = email_match.group(0).lower() if email_match else from_raw

                # Extract display name
                name_match = re.match(r'^"?([^"<]+)"?\s*<', from_raw)
                from_name  = name_match.group(1).strip() if name_match else from_email

                results.append({
                    "gmail_id":   msg_id,
                    "thread_id":  msg.get("threadId", ""),
                    "from_email": from_email,
                    "from_name":  from_name,
                    "subject":    subject,
                    "body":       body,
                    "date":       date,
                })

                # Mark as read (remove UNREAD label)
                service.users().messages().modify(
                    userId="me",
                    id=msg_id,
                    body={"removeLabelIds": ["UNREAD"]},
                ).execute()

            except Exception as e:
                log("GmailClient", f"Failed to fetch message {msg_id}: {e}", "ERROR")
                continue

    except HttpError as e:
        log("GmailClient", f"HttpError fetching messages: {e}", "ERROR")
    except Exception as e:
        log("GmailClient", f"Error fetching messages: {e}", "ERROR")

    return results


def get_thread_messages(thread_id: str) -> list[dict]:
    """
    Fetch all messages in a Gmail thread (full conversation history).
    Returns list of {from_email, body, date} ordered oldest→newest.
    """
    results = []
    try:
        service  = get_gmail_service()
        thread   = service.users().threads().get(
            userId="me", id=thread_id, format="full"
        ).execute()

        for msg in thread.get("messages", []):
            headers    = msg.get("payload", {}).get("headers", [])
            from_raw   = _parse_header(headers, "From")
            date       = _parse_header(headers, "Date")
            body       = _decode_body(msg.get("payload", {}))
            email_m    = re.search(r"[\w.+\-]+@[\w\-]+\.[a-zA-Z]+", from_raw)
            from_email = email_m.group(0).lower() if email_m else from_raw

            results.append({
                "gmail_id":   msg.get("id", ""),
                "from_email": from_email,
                "body":       body,
                "date":       date,
            })
    except Exception as e:
        log("GmailClient", f"Thread fetch error ({thread_id}): {e}", "ERROR")

    return results


# ── Email templates (mirrors email_sender.py but via Gmail API) ───────────────

def send_initial_outreach(
    to_addr:  str,
    name:     str,
    job_role: str,
    question: str,
) -> dict:
    """Send the Round 1 screening email."""
    subject = f"Your Application for {job_role} — Next Step"
    plain   = (
        f"Hi {name},\n\n"
        f"Thanks for applying for the {job_role} role. "
        "We reviewed your profile and would love to learn more about you.\n\n"
        f"{question}\n\n"
        "Looking forward to hearing from you.\n\nThe Hiring Team"
    )
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;color:#222;line-height:1.6">
      <p>Hi <strong>{name}</strong>,</p>
      <p>Thanks for applying for the <strong>{job_role}</strong> role.
         We reviewed your profile and would love to learn more.</p>
      <blockquote style="border-left:3px solid #f5a623;padding:8px 16px;color:#555;margin:16px 0">
        {question}
      </blockquote>
      <p>Looking forward to your reply.</p>
      <p style="color:#888;font-size:13px">— The Hiring Team</p>
    </div>"""
    return send_email(to_addr, subject, plain, html)


def send_rejection(to_addr: str, name: str, job_role: str) -> dict:
    """Send a respectful rejection email."""
    subject = f"Re: Your Application for {job_role}"
    plain   = (
        f"Hi {name},\n\nThank you for your interest in the {job_role} position. "
        "After careful review, we will not be moving forward at this time. "
        "We wish you the very best.\n\nThe Hiring Team"
    )
    return send_email(to_addr, subject, plain)
