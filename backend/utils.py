"""
backend/utils.py — Shared Utility Functions
Small, reusable helpers imported by agents and tools.
No heavy dependencies — keeps agents clean.
"""

import re
import json
import hashlib
import time
import functools
import sys
from datetime import datetime
from typing import Any


# ── Text Helpers 

def extract_email_address(raw: str) -> str:
    """
    Extract a plain email address from strings like 'John Doe <john@example.com>'.
    Returns the raw string stripped if no angle-bracket format is found.
    """
    match = re.search(r"[\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,}", raw or "")
    return match.group(0).lower() if match else raw.strip().lower()


def word_count(text: str) -> int:
    """Count words in a string, ignoring leading/trailing whitespace."""
    return len(text.split()) if text and text.strip() else 0


def strip_markdown_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` code fences from LLM output."""
    return re.sub(r"```[a-z]*\n?", "", text).replace("```", "").strip()


def safe_json_loads(text: str, fallback: Any = None) -> Any:
    """
    Parse JSON from an LLM response, tolerating markdown fences.
    Returns `fallback` on any parse error.
    """
    try:
        return json.loads(strip_markdown_fences(text))
    except (json.JSONDecodeError, TypeError):
        return fallback


def truncate(text: str, max_chars: int = 300, suffix: str = "…") -> str:
    """Truncate a string to max_chars and append suffix if truncated."""
    if not text:
        return ""
    text = str(text)
    return text if len(text) <= max_chars else text[:max_chars] + suffix


def normalise_whitespace(text: str) -> str:
    """Collapse multiple spaces/newlines into single spaces."""
    return re.sub(r"\s+", " ", (text or "").strip())


# ── Data Helpers 

def flatten_answers(answers: dict) -> str:
    """Concatenate all answer values into one string for bulk text analysis."""
    if not answers:
        return ""
    return " ".join(str(v) for v in answers.values() if v)


def candidate_fingerprint(candidate: dict) -> str:
    """
    Create a stable SHA-256 hash for a candidate based on email + name.
    Used to detect duplicates across uploads.
    """
    key = (candidate.get("email", "") + candidate.get("name", "")).lower().strip()
    return hashlib.sha256(key.encode()).hexdigest()


def score_colour(score: float) -> str:
    """Return a terminal-friendly colour label for a 0–100 score."""
    if score >= 70:  return "green"
    if score >= 50:  return "blue"
    if score >= 35:  return "yellow"
    return "red"


def tier_from_score(score: float) -> str:
    """Map a 0–100 score to a tier label (mirrors scoring_agent logic)."""
    if score >= 70: return "Fast-Track"
    if score >= 50: return "Standard"
    if score >= 35: return "Review"
    return "Reject"


# ── Time Helpers 

def seconds_since(dt: datetime) -> int:
    """Return whole seconds elapsed since a UTC datetime."""
    if dt is None:
        return -1
    return int((datetime.utcnow() - dt).total_seconds())


def human_duration(seconds: int) -> str:
    """Convert seconds to a human-readable string like '2h 15m'."""
    if seconds < 0:
        return "unknown"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m}m"


# ── Retry Decorator 

def retry(times: int = 3, delay: float = 1.0, exceptions=(Exception,)):
    """
    Decorator — retry a function up to `times` on specified exceptions.

    Usage:
        @retry(times=3, delay=2.0, exceptions=(requests.Timeout,))
        def fetch_github_profile(url): ...
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, times + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt < times:
                        time.sleep(delay * attempt)  # exponential back-off
            raise last_exc
        return wrapper
    return decorator


# ── Logging Helper 

def log(tag: str, message: str, level: str = "INFO") -> None:
    """
    Minimal structured console logger.
    Replace with Python logging module for production.
    """
    ts = datetime.utcnow().strftime("%H:%M:%S")
    line = f"[{ts}] [{level}] [{tag}] {message}"
    try:
        print(line)
    except UnicodeEncodeError:
        safe = line.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8",
            errors="replace",
        )
        print(safe)
