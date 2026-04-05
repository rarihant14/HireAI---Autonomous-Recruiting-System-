"""
backend/tasks.py - Celery task wrappers for long-running pipeline work.
"""

from __future__ import annotations

from backend.orchestrator import (
    ingest_batch,
    maybe_run_learning_cycle,
    process_incoming_emails_once,
    run_learning_now,
)
from backend.queue import celery_app


if celery_app is not None:

    @celery_app.task(name="hireai.ingest_batch")
    def ingest_batch_task(candidates: list[dict], job_role: str = "Software Engineer") -> dict:
        results = ingest_batch(candidates, job_role)
        ok = sum(1 for r in results if r["status"] in ("ingested", "duplicate"))
        return {
            "status": "completed",
            "ingested": ok,
            "total": len(results),
            "details": results[:10],
        }


    @celery_app.task(name="hireai.run_learning")
    def run_learning_task() -> dict:
        result = run_learning_now()
        return {
            "status": "completed",
            "latest": result,
        }


    @celery_app.task(name="hireai.poll_email_once")
    def poll_email_once_task() -> dict:
        processed = process_incoming_emails_once()
        return {
            "status": "completed",
            "processed_replies": processed,
        }


    @celery_app.task(name="hireai.maybe_run_learning")
    def maybe_run_learning_task() -> dict:
        result = maybe_run_learning_cycle()
        if "status" not in result:
            result["status"] = "completed"
        return result
