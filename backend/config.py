"""
backend/config.py - Centralized Configuration
All environment variables are read here once and exposed as typed attributes.
Import this instead of calling os.getenv() scattered across the codebase.

Usage:
    from backend.config import cfg
    print(cfg.GROQ_MODEL)
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
_PROJECT_ROOT = Path(__file__).parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=False)


@dataclass(frozen=True)
class Config:
    # Groq / LLM
    GROQ_API_KEY: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", ""))
    GROQ_MODEL: str = field(default_factory=lambda: os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"))
    GROQ_TEMPERATURE: float = field(default_factory=lambda: float(os.getenv("GROQ_TEMPERATURE", "0.3")))
    GROQ_MAX_TOKENS: int = field(default_factory=lambda: int(os.getenv("GROQ_MAX_TOKENS", "4096")))

    # Email / Gmail API + SMTP fallback
    SMTP_HOST: str = field(default_factory=lambda: os.getenv("SMTP_HOST", "smtp.gmail.com"))
    SMTP_PORT: int = field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587")))
    SMTP_USER: str = field(default_factory=lambda: os.getenv("SMTP_USER", ""))
    SMTP_PASSWORD: str = field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))

    # Queue / Celery
    CELERY_BROKER_URL: str = field(default_factory=lambda: os.getenv("CELERY_BROKER_URL", ""))
    CELERY_RESULT_BACKEND: str = field(default_factory=lambda: os.getenv("CELERY_RESULT_BACKEND", ""))
    CELERY_TIMEZONE: str = field(default_factory=lambda: os.getenv("CELERY_TIMEZONE", "UTC"))
    CELERY_BEAT_POLLING_ENABLED: bool = field(default_factory=lambda: os.getenv("CELERY_BEAT_POLLING_ENABLED", "true").lower() == "true")
    CELERY_BEAT_LEARNING_ENABLED: bool = field(default_factory=lambda: os.getenv("CELERY_BEAT_LEARNING_ENABLED", "true").lower() == "true")

    # Database
    DATABASE_URL: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///hiring_system.db"))

    # Flask
    FLASK_HOST: str = field(default_factory=lambda: os.getenv("FLASK_HOST", "127.0.0.1"))
    FLASK_PORT: int = field(default_factory=lambda: int(os.getenv("FLASK_PORT", "5000")))
    FLASK_DEBUG: bool = field(default_factory=lambda: os.getenv("FLASK_DEBUG", "false").lower() == "true")

    # System Behavior
    EMAIL_POLL_INTERVAL: int = field(default_factory=lambda: int(os.getenv("EMAIL_POLL_INTERVAL", "120")))
    LEARNING_CHECK_INTERVAL: int = field(default_factory=lambda: int(os.getenv("LEARNING_CHECK_INTERVAL", "300")))
    AI_SIMILARITY_THRESHOLD: float = field(default_factory=lambda: float(os.getenv("AI_SIMILARITY_THRESHOLD", "0.80")))
    COPY_SIMILARITY_THRESHOLD: float = field(default_factory=lambda: float(os.getenv("COPY_SIMILARITY_THRESHOLD", "0.60")))
    SUSPICIOUS_REPLY_SECONDS: int = field(default_factory=lambda: int(os.getenv("SUSPICIOUS_REPLY_SECONDS", "120")))
    MAX_STRIKES: int = field(default_factory=lambda: int(os.getenv("MAX_STRIKES", "3")))

    def validate(self) -> list[str]:
        """Return a list of warnings for missing critical settings."""
        warnings = []
        if not self.GROQ_API_KEY or self.GROQ_API_KEY == "your_groq_api_key_here":
            warnings.append("GROQ_API_KEY is not set - AI features will fail.")

        has_gmail_api = (_PROJECT_ROOT / "credentials.json").exists() or (_PROJECT_ROOT / "token.json").exists()
        has_smtp_fallback = bool(self.SMTP_USER and self.SMTP_PASSWORD)

        if not has_gmail_api and not has_smtp_fallback:
            warnings.append("No Gmail API credentials or SMTP fallback configured - email sending is disabled.")
        elif self.SMTP_USER and not self.SMTP_PASSWORD:
            warnings.append("SMTP_PASSWORD is not set - SMTP fallback is disabled.")
        elif self.SMTP_PASSWORD and not self.SMTP_USER:
            warnings.append("SMTP_USER is not set - SMTP fallback is disabled.")
        if self.CELERY_RESULT_BACKEND and not self.CELERY_BROKER_URL:
            warnings.append("CELERY_RESULT_BACKEND is set without CELERY_BROKER_URL - the queue is disabled.")
        return warnings


cfg = Config()
