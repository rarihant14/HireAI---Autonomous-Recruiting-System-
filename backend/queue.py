"""
backend/queue.py - Optional Celery integration for background jobs.

The app keeps working without Celery installed or configured. When Redis and
Celery are available, heavy jobs can be pushed to a worker instead of running
inside the Flask request thread.
"""

from __future__ import annotations

import os
from typing import Optional

try:
    from celery import Celery
except Exception:
    Celery = None


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _env_bool(name: str, default: str = "false") -> bool:
    return _env(name, default).lower() in {"1", "true", "yes", "on"}


def queue_enabled() -> bool:
    """Return True when Celery is installed and a broker URL is configured."""
    return Celery is not None and bool(_env("CELERY_BROKER_URL"))


def create_celery() -> Optional["Celery"]:
    if not queue_enabled():
        return None

    broker_url = _env("CELERY_BROKER_URL")
    result_backend = _env("CELERY_RESULT_BACKEND", broker_url)

    app = Celery(
        "hireai",
        broker=broker_url,
        backend=result_backend,
        include=["backend.tasks"],
    )
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        task_track_started=True,
        timezone=_env("CELERY_TIMEZONE", "UTC"),
        enable_utc=True,
    )

    beat_schedule = {}
    if _env_bool("CELERY_BEAT_POLLING_ENABLED", "true"):
        beat_schedule["poll-email-inbox"] = {
            "task": "hireai.poll_email_once",
            "schedule": float(_env("EMAIL_POLL_INTERVAL", "120")),
        }
    if _env_bool("CELERY_BEAT_LEARNING_ENABLED", "true"):
        beat_schedule["maybe-run-learning"] = {
            "task": "hireai.maybe_run_learning",
            "schedule": float(_env("LEARNING_CHECK_INTERVAL", "300")),
        }
    if beat_schedule:
        app.conf.beat_schedule = beat_schedule

    return app


celery_app = create_celery()
