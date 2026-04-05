import os
import shutil
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from backend import database
from backend.database import AntiCheatLog, Candidate, Interaction, ReviewNote, SystemLearning
from backend import orchestrator
from backend import queue as queue_module
from backend.queue import create_celery


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


class BaseDatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = Path("tests") / ".tmp" / self._testMethodName
        self.tempdir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.tempdir / "test.db"
        self.env_patcher = patch.dict(
            os.environ,
            {
                "DATABASE_URL": _sqlite_url(self.db_path),
                "CELERY_BROKER_URL": "",
                "CELERY_RESULT_BACKEND": "",
                "MAX_STRIKES": "3",
            },
            clear=False,
        )
        self.env_patcher.start()
        orchestrator._engine = None
        self.engine = database.get_engine(os.environ["DATABASE_URL"])
        database.init_db(self.engine)
        orchestrator._engine = self.engine

    def tearDown(self):
        if getattr(self, "engine", None) is not None:
            self.engine.dispose()
        orchestrator._engine = None
        self.env_patcher.stop()
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def session(self):
        return database.get_session(orchestrator.get_engine())


class CopyRingTests(BaseDatabaseTestCase):
    def test_ingestion_copy_ring_adds_strikes_to_all_cluster_members(self):
        shared_answer = "I built a FastAPI service with Redis, Docker, and retry handling for failures."
        score_result = {
            "final_score": 55.0,
            "breakdown": {},
            "tier": "Reject",
            "notes": [],
        }

        with (
            patch("backend.orchestrator.score_candidate", return_value=score_result),
            patch("backend.orchestrator._check_candidate_answers_for_ai", return_value=None),
            patch("backend.orchestrator._enqueue_task", return_value=None),
        ):
            for idx in range(3):
                result = orchestrator.ingest_candidate(
                    {
                        "name": f"Candidate {idx}",
                        "email": f"candidate{idx}@example.com",
                        "skills": "Python, FastAPI",
                        "answers": {"Describe a relevant project": shared_answer},
                    },
                    job_role="Software Engineer",
                )
                self.assertEqual(result["status"], "ingested")

        session = self.session()
        try:
            candidates = session.query(Candidate).order_by(Candidate.email).all()
            self.assertEqual(len(candidates), 3)
            for candidate in candidates:
                self.assertEqual(candidate.copy_flag_count, 1)
                self.assertEqual(candidate.total_strikes, 1)
                self.assertFalse(candidate.is_eliminated)

            logs = session.query(AntiCheatLog).filter_by(check_type="COPY_RING").all()
            self.assertEqual(len(logs), 3)
            self.assertTrue(all("APPLICATION_ANSWER" in log.details for log in logs))
        finally:
            session.close()

    def test_reply_copy_ring_can_eliminate_cluster_at_threshold(self):
        with patch.dict(os.environ, {"MAX_STRIKES": "1"}, clear=False):
            session = self.session()
            try:
                body = "We should use FastAPI, Redis caching, and Docker so the service stays observable and easy to deploy."
                sent_at = datetime.utcnow() - timedelta(minutes=20)
                candidates = [
                    Candidate(name="A", email="a@example.com", answers={}, current_round=1, last_email_sent_at=sent_at),
                    Candidate(name="B", email="b@example.com", answers={}, current_round=1, last_email_sent_at=sent_at),
                    Candidate(name="C", email="c@example.com", answers={}, current_round=1, last_email_sent_at=sent_at),
                ]
                session.add_all(candidates)
                session.flush()
                session.add_all([
                    Interaction(candidate_id=candidates[1].id, direction="received", body=body, subject="Re: test", round_number=1),
                    Interaction(candidate_id=candidates[2].id, direction="received", body=body, subject="Re: test", round_number=1),
                ])
                session.commit()
            finally:
                session.close()

            with (
                patch("backend.agents.engagement_agent.fetch_new_replies", return_value=[{
                    "from": "a@example.com",
                    "subject": "Re: test",
                    "body": body,
                    "thread_id": "thread-1",
                }]),
                patch("backend.agents.engagement_agent.process_candidate_reply", return_value={"next_email_body": ""}),
            ):
                processed = orchestrator.process_incoming_emails_once()

            self.assertEqual(processed, 1)

            session = self.session()
            try:
                refreshed = session.query(Candidate).order_by(Candidate.email).all()
                self.assertEqual(len(refreshed), 3)
                for candidate in refreshed:
                    self.assertEqual(candidate.copy_flag_count, 1)
                    self.assertEqual(candidate.total_strikes, 1)
                    self.assertTrue(candidate.is_eliminated)

                logs = session.query(AntiCheatLog).filter_by(check_type="COPY_RING").all()
                self.assertEqual(len(logs), 3)
                self.assertTrue(all("EMAIL_REPLY" in log.details for log in logs))
            finally:
                session.close()

    def test_reply_reviews_are_saved_in_dedicated_review_notes_table(self):
        session = self.session()
        try:
            candidate = Candidate(
                name="Reviewer",
                email="reviewer@example.com",
                answers={"Q1": "Initial answer"},
                current_round=1,
                last_email_sent_at=datetime.utcnow() - timedelta(minutes=10),
            )
            session.add(candidate)
            session.commit()
        finally:
            session.close()

        with (
            patch("backend.agents.engagement_agent.fetch_new_replies", return_value=[{
                "from": "reviewer@example.com",
                "subject": "Re: round 1",
                "body": "Here is my code answer",
                "thread_id": "thread-review",
            }]),
            patch("backend.agents.engagement_agent.process_candidate_reply", return_value={
                "next_email_body": "",
                "ai_review": "Looks polished and likely AI-assisted.",
                "ai_score": 0.82,
                "code_feedback": "Python snippet compiles but needs edge-case handling.",
            }),
            patch("backend.agents.anti_cheat_agent.check_candidate_response", return_value={
                "strikes": 0,
                "flags": [],
                "ai_score": 0.0,
                "ai_flagged": False,
                "timing_flagged": False,
                "ai_explanation": "",
            }),
        ):
            processed = orchestrator.process_incoming_emails_once()

        self.assertEqual(processed, 1)

        session = self.session()
        try:
            notes = session.query(ReviewNote).order_by(ReviewNote.review_type).all()
            self.assertEqual(len(notes), 2)
            self.assertEqual([note.review_type for note in notes], ["AI_REVIEW", "CODE_REVIEW"])
            self.assertTrue(any("AI-assisted" in note.summary for note in notes))
            self.assertTrue(any("compiles" in note.summary for note in notes))

            logs = session.query(AntiCheatLog).filter(
                AntiCheatLog.check_type.in_(["AI_REVIEW", "CODE_REVIEW"])
            ).all()
            self.assertEqual(len(logs), 0)
        finally:
            session.close()


class LearningScheduleTests(BaseDatabaseTestCase):
    def test_maybe_run_learning_cycle_runs_when_new_interval_reached(self):
        session = self.session()
        try:
            for idx in range(10):
                session.add(Candidate(name=f"Candidate {idx}", email=f"candidate{idx}@example.com"))
            session.commit()
        finally:
            session.close()

        expected = {"status": "completed", "candidate_count": 10}
        with patch("backend.orchestrator.run_learning_now", return_value=expected) as mock_run:
            result = orchestrator.maybe_run_learning_cycle()

        self.assertEqual(result, expected)
        mock_run.assert_called_once()

    def test_maybe_run_learning_cycle_skips_when_latest_learning_is_current(self):
        session = self.session()
        try:
            for idx in range(10):
                session.add(Candidate(name=f"Candidate {idx}", email=f"candidate{idx}@example.com"))
            session.add(SystemLearning(candidate_count=10, insights=[], pattern_updates={}, raw_report="ok"))
            session.commit()
        finally:
            session.close()

        with patch("backend.orchestrator.run_learning_now") as mock_run:
            result = orchestrator.maybe_run_learning_cycle()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["candidate_count"], 10)
        self.assertEqual(result["latest_learning_count"], 10)
        mock_run.assert_not_called()

    def test_create_celery_configures_beat_schedule_for_polling_and_learning(self):
        if queue_module.Celery is None:
            self.skipTest("Celery is not importable in this environment")

        with patch.dict(
            os.environ,
            {
                "CELERY_BROKER_URL": "redis://localhost:6379/0",
                "CELERY_RESULT_BACKEND": "redis://localhost:6379/0",
                "CELERY_BEAT_POLLING_ENABLED": "true",
                "CELERY_BEAT_LEARNING_ENABLED": "true",
                "EMAIL_POLL_INTERVAL": "123",
                "LEARNING_CHECK_INTERVAL": "456",
            },
            clear=False,
        ):
            app = create_celery()

        self.assertIsNotNone(app)
        beat_schedule = app.conf.beat_schedule
        self.assertIn("poll-email-inbox", beat_schedule)
        self.assertIn("maybe-run-learning", beat_schedule)
        self.assertEqual(beat_schedule["poll-email-inbox"]["task"], "hireai.poll_email_once")
        self.assertEqual(beat_schedule["maybe-run-learning"]["task"], "hireai.maybe_run_learning")
        self.assertEqual(float(beat_schedule["poll-email-inbox"]["schedule"]), 123.0)
        self.assertEqual(float(beat_schedule["maybe-run-learning"]["schedule"]), 456.0)

    def test_get_stats_includes_learning_status_payload(self):
        session = self.session()
        try:
            for idx in range(10):
                session.add(Candidate(name=f"Candidate {idx}", email=f"candidate{idx}@example.com"))
            session.commit()
        finally:
            session.close()

        stats = orchestrator.get_stats()

        self.assertIn("learning_status", stats)
        self.assertEqual(stats["learning_status"]["candidate_count"], 10)
        self.assertEqual(stats["learning_status"]["latest_learning_count"], 0)
        self.assertTrue(stats["learning_status"]["learning_due"])
        self.assertIn("current_weights", stats["learning_status"])


if __name__ == "__main__":
    unittest.main()
