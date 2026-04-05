"""
backend/orchestrator.py — Component 6: Integration
Ties all agents together. Each function is wrapped so ONE candidate
failing never blocks the rest of the batch.
"""

import os
import threading
import time
from datetime import datetime

from backend.database import (
    init_db, get_session, Candidate, Interaction, AntiCheatLog,
    ReviewNote, SystemLearning, get_scoring_weights, save_scoring_weights,
)
from backend.agents.scoring_agent    import score_candidate
from backend.agents.learning_agent   import run_learning_cycle, should_run_analysis
from backend.queue import queue_enabled
from backend.tools.similarity import find_copy_rings
from backend.utils import log, seconds_since

_engine = None

def get_engine():
    global _engine
    if _engine is None:
        _engine = init_db()
    return _engine


def _normalize_learning_weights(pattern_updates: dict | None, current_weights: dict) -> dict:
    """
    Clamp, fill, and normalize learned weights so scoring can apply them safely.
    """
    allowed_ranges = {
        "technical_skills": (0.0, 0.4),
        "answer_quality": (0.0, 0.4),
        "github_quality": (0.0, 0.3),
        "ai_penalty": (0.0, 0.3),
        "completeness": (0.0, 0.2),
    }
    merged = current_weights.copy()
    for key, value in (pattern_updates or {}).items():
        if key not in allowed_ranges:
            continue
        low, high = allowed_ranges[key]
        try:
            merged[key] = min(high, max(low, float(value)))
        except (TypeError, ValueError):
            continue

    total = sum(merged.values())
    if total <= 0:
        return current_weights.copy()
    return {key: round(value / total, 4) for key, value in merged.items()}


def _enqueue_task(task_name: str, *args):
    """Best-effort Celery dispatch without hard dependency on Celery at runtime."""
    if not queue_enabled():
        return None

    try:
        from backend.queue import celery_app

        if celery_app is None:
            return None
        return celery_app.send_task(task_name, args=list(args))
    except Exception as e:
        log("Orchestrator", f"Queue dispatch failed for {task_name}: {e}", "WARN")
        return None

def _enforce_strike_limit(candidate: Candidate) -> None:
    max_strikes = int(os.getenv("MAX_STRIKES", "3"))
    if candidate.total_strikes >= max_strikes:
        candidate.is_eliminated = True


def _apply_copy_ring_logs(
    session,
    cluster_ids: set[int],
    reference_id: int,
    question_label: str,
    source: str,
    max_similarity: float,
) -> int:
    """
    Log one copy-ring event for each member of a 3+ candidate cluster.
    Returns how many new logs were created.
    """
    signature = f"SIGNATURE:{source}|{question_label}|{','.join(map(str, sorted(cluster_ids)))}"
    created = 0

    for cid in cluster_ids:
        existing = (
            session.query(AntiCheatLog)
            .filter_by(candidate_id=cid, check_type="COPY_RING")
            .filter(AntiCheatLog.details.contains(signature))
            .first()
        )
        if existing:
            continue

        candidate = session.get(Candidate, cid)
        if not candidate:
            continue

        peer_ids = [str(peer_id) for peer_id in sorted(cluster_ids) if peer_id != cid]
        session.add(AntiCheatLog(
            candidate_id=cid,
            check_type="COPY_RING",
            similarity_score=max_similarity,
            details=(
                f"{signature}\n"
                f"Source: {source}\n"
                f"Question: {question_label}\n"
                f"Similar candidates: {', '.join(peer_ids) or 'none'}\n"
                f"Cluster size: {len(cluster_ids)}\n"
                f"Peak similarity: {max_similarity:.3f}\n"
                f"Reference candidate: {reference_id}"
            ),
        ))
        candidate.copy_flag_count += 1
        candidate.total_strikes += 1
        _enforce_strike_limit(candidate)
        created += 1
    return created


def _log_review_event(
    session,
    candidate_id: int,
    review_type: str,
    details: str,
    score: float = 0.0,
) -> None:
    """Persist non-strike review details so the UI can show them later."""
    cleaned = (details or "").strip()
    if not cleaned:
        return

    existing = (
        session.query(ReviewNote)
        .filter_by(candidate_id=candidate_id, review_type=review_type)
        .filter(ReviewNote.summary == cleaned)
        .first()
    )
    if existing:
        return

    session.add(ReviewNote(
        candidate_id=candidate_id,
        review_type=review_type,
        score=score,
        summary=cleaned,
    ))


def _detect_copy_ring_clusters(entries: list[dict], reference_id: int) -> tuple[set[int], float]:
    detections = find_copy_rings(
        entries,
        threshold=float(os.getenv("COPY_SIMILARITY_THRESHOLD", "0.60")),
    )
    if not detections:
        return set(), 0.0

    graph: dict[int, set[int]] = {}
    max_similarity = 0.0
    for item in detections:
        a = int(item["candidate_a"])
        b = int(item["candidate_b"])
        graph.setdefault(a, set()).add(b)
        graph.setdefault(b, set()).add(a)
        max_similarity = max(max_similarity, float(item.get("similarity", 0.0)))

    if reference_id not in graph:
        return set(), 0.0

    cluster = set()
    stack = [reference_id]
    while stack:
        current = stack.pop()
        if current in cluster:
            continue
        cluster.add(current)
        stack.extend(graph.get(current, set()) - cluster)
    return (cluster, round(max_similarity, 3)) if len(cluster) >= 3 else (set(), 0.0)


def _check_candidate_answers_for_ai(session, candidate: Candidate) -> None:
    """Run anti-cheat checks on submitted application answers during ingestion."""
    answers = candidate.answers or {}
    if not answers:
        return

    try:
        from backend.agents.anti_cheat_agent import check_candidate_response
    except Exception as e:
        log("Orchestrator", f"Anti-cheat import skipped during ingest for {candidate.email}: {e}", "WARN")
        return

    for question, answer in answers.items():
        answer_text = (answer or "").strip()
        if not answer_text:
            continue

        try:
            cheat = check_candidate_response(candidate.id, question, answer_text, -1)
        except Exception as e:
            log("Orchestrator", f"Anti-cheat answer check failed for {candidate.email}: {e}", "WARN")
            continue

        if not cheat["ai_flagged"] and not cheat["timing_flagged"] and not cheat["flags"]:
            continue

        session.add(AntiCheatLog(
            candidate_id=candidate.id,
            check_type="APPLICATION_ANSWER",
            similarity_score=cheat["ai_score"],
            details=(
                f"Question: {question}\n"
                f"Answer: {answer_text[:300]}\n"
                f"Assessment: {cheat['ai_explanation']}"
            ),
        ))
        candidate.total_strikes += cheat["strikes"]
        candidate.ai_flag_count += 1 if cheat["ai_flagged"] else 0
        candidate.timing_flag_count += 1 if cheat["timing_flagged"] else 0

    _enforce_strike_limit(candidate)


def _check_candidate_answers_for_copy_rings(session, candidate: Candidate) -> None:
    """Compare a new candidate's application answers against prior candidates."""
    answers = candidate.answers or {}
    if not answers:
        return

    existing_candidates = (
        session.query(Candidate)
        .filter(Candidate.id != candidate.id)
        .all()
    )

    for question, answer in answers.items():
        answer_text = (answer or "").strip()
        if not answer_text:
            continue

        entries = [{"id": candidate.id, "answer": answer_text}]
        for other in existing_candidates:
            other_answer = (other.answers or {}).get(question, "")
            if other_answer and str(other_answer).strip():
                entries.append({"id": other.id, "answer": str(other_answer).strip()})

        cluster_ids, max_similarity = _detect_copy_ring_clusters(entries, candidate.id)
        if cluster_ids:
            _apply_copy_ring_logs(
                session,
                cluster_ids=cluster_ids,
                reference_id=candidate.id,
                question_label=question,
                source="APPLICATION_ANSWER",
                max_similarity=max_similarity,
            )

# ── Ingest one candidate 

def ingest_candidate(candidate_dict: dict, job_role: str = "Software Engineer") -> dict:
    """
    Score → save to DB → optionally send Round 1 email.
    Returns a result dict — NEVER raises, so batch ingestion never stalls.
    """
    session = get_session(get_engine())
    try:
        email = (candidate_dict.get("email") or "").strip().lower()

        # Skip rows with no email (can't track or email them)
        if not email:
            return {"status": "skipped", "reason": "no email address"}

        # Duplicate check
        existing = session.query(Candidate).filter_by(email=email).first()
        if existing:
            return {"status": "duplicate", "candidate_id": existing.id,
                    "score": existing.total_score, "tier": existing.tier}

        # Score — always succeeds (has nuclear fallback)
        scored = score_candidate(candidate_dict)

        # Persist to DB
        c = Candidate(
            name             = (candidate_dict.get("name") or "").strip(),
            email            = email,
            phone            = (candidate_dict.get("phone") or "").strip(),
            college          = (candidate_dict.get("college") or "").strip(),
            github_url       = (candidate_dict.get("github_url") or "").strip(),
            resume_url       = (candidate_dict.get("resume_url") or "").strip(),
            skills           = (candidate_dict.get("skills") or "").strip(),
            answers          = candidate_dict.get("answers") or {},
            raw_data         = candidate_dict,
            total_score      = scored["final_score"],
            score_breakdown  = scored["breakdown"],
            tier             = scored["tier"],
        )
        session.add(c)
        session.flush()   # get c.id before commit

        _check_candidate_answers_for_ai(session, c)
        _check_candidate_answers_for_copy_rings(session, c)

        # Send Round 1 email only if Gmail API is configured and tier is not Reject
        email_sent = False
        if scored["tier"] != "Reject" and not c.is_eliminated:
            try:
                from backend.agents.engagement_agent import send_initial_email
                result = send_initial_email(
                    candidate_name  = c.name or "Candidate",
                    candidate_email = email,
                    job_role        = job_role,
                )
                email_sent = result.get("success", False)
                if email_sent:
                    c.current_round      = 1
                    c.last_email_sent_at = datetime.utcnow()
            except Exception as e:
                log("Orchestrator", f"Email send skipped for {email}: {e}", "WARN")

        session.commit()
        candidate_id = c.id

        # Trigger self-learning every 10 candidates.
        total = session.query(Candidate).count()
        if should_run_analysis(total):
            if _enqueue_task("hireai.maybe_run_learning") is None:
                threading.Thread(target=maybe_run_learning_cycle, daemon=True).start()

        log("Orchestrator", f"Ingested {email} → {scored['tier']} ({scored['final_score']:.1f})")
        return {
            "status":       "ingested",
            "candidate_id": candidate_id,
            "score":        scored["final_score"],
            "tier":         scored["tier"],
            "email_sent":   email_sent,
            "notes":        scored.get("notes", []),
        }

    except Exception as e:
        session.rollback()
        log("Orchestrator", f"ingest_candidate failed for {candidate_dict.get('email','?')}: {e}", "ERROR")
        return {"status": "error", "error": str(e),
                "email": candidate_dict.get("email", "?"), "score": None, "tier": None}
    finally:
        session.close()


def ingest_batch(candidates: list, job_role: str = "Software Engineer") -> list:
    """Ingest a list of candidates. Each is independent — one error won't stop others."""
    results = []
    for i, c in enumerate(candidates):
        try:
            r = ingest_candidate(c, job_role)
        except Exception as e:
            r = {"status": "error", "error": str(e), "email": c.get("email", "?")}
        results.append(r)
        # Small sleep every 10 to avoid hammering GitHub API
        if (i + 1) % 10 == 0:
            time.sleep(0.5)
    return results

# ── Email polling loop 

def process_incoming_emails_once() -> int:
    session = get_session(get_engine())
    processed = 0
    try:
        from backend.agents.engagement_agent import fetch_new_replies, process_candidate_reply
        replies = fetch_new_replies()
        for reply in replies:
            sender_email = reply.get("from", "").lower().strip()
            candidate = session.query(Candidate).filter_by(email=sender_email).first()
            if not candidate or candidate.is_eliminated:
                continue

            processed += 1

            body = reply.get("body", "")
            latency = seconds_since(candidate.last_email_sent_at)

            # Log incoming interaction
            session.add(Interaction(
                candidate_id          = candidate.id,
                direction             = "received",
                subject               = reply.get("subject", ""),
                body                  = body,
                thread_id             = reply.get("thread_id", ""),
                round_number          = candidate.current_round,
                reply_latency_seconds = latency,
            ))

            # Anti-cheat (non-blocking)
            try:
                from backend.agents.anti_cheat_agent import check_candidate_response
                for q in (candidate.answers or {}).keys():
                    cheat = check_candidate_response(candidate.id, q, body, latency)
                    if cheat["strikes"] > 0:
                        session.add(AntiCheatLog(
                            candidate_id     = candidate.id,
                            check_type       = ",".join(cheat["flags"]),
                            similarity_score = cheat["ai_score"],
                            details          = cheat["ai_explanation"],
                        ))
                        candidate.total_strikes  += cheat["strikes"]
                        candidate.ai_flag_count  += 1 if cheat["ai_flagged"] else 0
            except Exception as e:
                log("Orchestrator", f"Anti-cheat error: {e}", "WARN")

            current_reply_entries = [{"id": candidate.id, "answer": body}]
            other_replies = (
                session.query(Interaction)
                .filter(
                    Interaction.direction == "received",
                    Interaction.candidate_id != candidate.id,
                    Interaction.round_number == candidate.current_round,
                )
                .all()
            )
            for interaction in other_replies:
                if interaction.body and interaction.body.strip():
                    current_reply_entries.append({
                        "id": interaction.candidate_id,
                        "answer": interaction.body.strip(),
                    })

            cluster_ids, max_similarity = _detect_copy_ring_clusters(current_reply_entries, candidate.id)
            if cluster_ids:
                _apply_copy_ring_logs(
                    session,
                    cluster_ids=cluster_ids,
                    reference_id=candidate.id,
                    question_label=f"Round {candidate.current_round} reply",
                    source="EMAIL_REPLY",
                    max_similarity=max_similarity,
                )

            _enforce_strike_limit(candidate)
            if candidate.is_eliminated:
                candidate.is_eliminated = True
                session.commit()
                continue

            # Build history and respond
            try:
                history = [
                    {"role": i.direction, "content": i.body}
                    for i in sorted(candidate.interactions, key=lambda x: x.id)
                ]
                eng = process_candidate_reply(
                    candidate_id         = candidate.id,
                    candidate_name       = candidate.name,
                    candidate_email      = candidate.email,
                    job_role             = "Software Engineer",
                    round_number         = candidate.current_round,
                    thread_id            = reply.get("thread_id", ""),
                    subject              = reply.get("subject", ""),
                    latest_reply         = body,
                    conversation_history = history,
                )
                _log_review_event(
                    session,
                    candidate_id=candidate.id,
                    review_type="AI_REVIEW",
                    details=eng.get("ai_review", ""),
                    score=float(eng.get("ai_score", 0.0) or 0.0),
                )
                _log_review_event(
                    session,
                    candidate_id=candidate.id,
                    review_type="CODE_REVIEW",
                    details=eng.get("code_feedback", ""),
                )
                if eng.get("next_email_body"):
                    session.add(Interaction(
                        candidate_id = candidate.id,
                        direction    = "sent",
                        subject      = eng.get("subject", ""),
                        body         = eng["next_email_body"],
                        thread_id    = reply.get("thread_id", ""),
                        round_number = candidate.current_round,
                    ))
                    candidate.current_round      = eng.get("round_number", candidate.current_round)
                    candidate.last_email_sent_at = datetime.utcnow()
                    candidate.last_reply_at      = datetime.utcnow()
            except Exception as e:
                log("Orchestrator", f"Engagement error: {e}", "WARN")

            session.commit()
    except Exception as e:
        log("Orchestrator", f"Email loop error: {e}", "ERROR")
        session.rollback()
    finally:
        session.close()
    return processed

# ── Background polling 

_polling_active = False

def start_email_polling():
    global _polling_active

    if queue_enabled():
        beat_enabled = os.getenv("CELERY_BEAT_POLLING_ENABLED", "true").lower() == "true"
        result = _enqueue_task("hireai.poll_email_once")
        if result is not None:
            log("Orchestrator", f"Queued one email poll cycle: {result.id}")
            return {
                "status": "queued",
                "task_id": result.id,
                "mode": "celery",
                "beat_enabled": beat_enabled,
                "message": (
                    "Immediate poll queued; Celery Beat will keep polling after restarts."
                    if beat_enabled else
                    "Immediate poll queued. Enable Celery Beat for persistent polling."
                ),
            }

    _polling_active = True
    interval = int(os.getenv("EMAIL_POLL_INTERVAL", "120"))

    def loop():
        while _polling_active:
            try:
                process_incoming_emails_once()
            except Exception as e:
                log("Orchestrator", f"Poll loop error: {e}", "ERROR")
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True).start()
    log("Orchestrator", f"Email polling started (every {interval}s)")
    return {"status": "started", "mode": "thread", "interval_seconds": interval}

def stop_email_polling():
    global _polling_active
    if queue_enabled():
        beat_enabled = os.getenv("CELERY_BEAT_POLLING_ENABLED", "true").lower() == "true"
        return {
            "status": "scheduled" if beat_enabled else "stopped",
            "mode": "celery",
            "message": (
                "Polling is managed by Celery Beat; disable CELERY_BEAT_POLLING_ENABLED to stop persistent polling."
                if beat_enabled else
                "No in-process polling loop is running."
            ),
        }
    _polling_active = False
    return {"status": "stopped"}

# ── Learning 

def run_learning_now() -> dict:
    session = get_session(get_engine())
    try:
        candidates = [
            {
                "id": c.id, "name": c.name, "final_score": c.total_score,
                "tier": c.tier, "skills_score": (c.score_breakdown or {}).get("skills_score", 0),
                "answer_score": (c.score_breakdown or {}).get("answer_score", 0),
                "github_score": (c.score_breakdown or {}).get("github_score", 0),
                "ai_flag_count": c.ai_flag_count, "copy_flag_count": c.copy_flag_count,
                "answers": c.answers or {},
            }
            for c in session.query(Candidate).all()
        ]
        interactions = [
            {"candidate_id": i.candidate_id, "direction": i.direction, "round": i.round_number}
            for i in session.query(Interaction).all()
        ]
        result = run_learning_cycle(candidates, interactions)
        current_weights = get_scoring_weights(session)
        applied_weights = _normalize_learning_weights(result.get("pattern_updates"), current_weights)
        save_scoring_weights(session, applied_weights)
        session.add(SystemLearning(
            candidate_count = len(candidates),
            insights        = result["insights"],
            pattern_updates = applied_weights,
            raw_report      = result["raw_report"],
        ))
        session.commit()
        log("Orchestrator", f"Learning cycle done. {len(result['insights'])} insights.")
        return {
            "generated_at": datetime.utcnow().isoformat(),
            "candidate_count": len(candidates),
            "insights": result["insights"],
            "pattern_updates": applied_weights,
        }
    except Exception as e:
        log("Orchestrator", f"Learning error: {e}", "ERROR")
        session.rollback()
        return {"error": str(e)}
    finally:
        session.close()


def maybe_run_learning_cycle() -> dict:
    """Run learning only when the candidate count has crossed the next interval boundary."""
    session = get_session(get_engine())
    try:
        total = session.query(Candidate).count()
        latest = (
            session.query(SystemLearning)
            .order_by(SystemLearning.generated_at.desc())
            .first()
        )
        latest_count = latest.candidate_count if latest else 0
        if should_run_analysis(total) and total > latest_count:
            return run_learning_now()
        return {
            "status": "skipped",
            "candidate_count": total,
            "latest_learning_count": latest_count,
        }
    finally:
        session.close()


def get_learning_status() -> dict:
    """Expose whether the next learning cycle is due and which weights are active."""
    session = get_session(get_engine())
    try:
        total = session.query(Candidate).count()
        latest = (
            session.query(SystemLearning)
            .order_by(SystemLearning.generated_at.desc())
            .first()
        )
        latest_count = latest.candidate_count if latest else 0
        return {
            "candidate_count": total,
            "latest_learning_count": latest_count,
            "learning_due": bool(should_run_analysis(total) and total > latest_count),
            "current_weights": get_scoring_weights(session),
            "last_generated_at": str(latest.generated_at) if latest else "",
        }
    finally:
        session.close()

# ── Query helpers 

def _repair_broken_scores(session) -> None:
    """
    Rescore candidates previously saved by the emergency scoring fallback.
    Those rows typically have 0.0 score, tier Review, and empty breakdown.
    """
    broken_rows = (
        session.query(Candidate)
        .filter(Candidate.total_score == 0.0, Candidate.tier == "Review")
        .all()
    )

    changed = False
    for c in broken_rows:
        if c.score_breakdown not in (None, {}, "{}", ""):
            continue

        source = c.raw_data or {
            "name": c.name,
            "email": c.email,
            "phone": c.phone,
            "college": c.college,
            "github_url": c.github_url,
            "resume_url": c.resume_url,
            "skills": c.skills,
            "answers": c.answers or {},
        }
        rescored = score_candidate(source)
        if rescored.get("breakdown"):
            c.total_score = rescored["final_score"]
            c.score_breakdown = rescored["breakdown"]
            c.tier = rescored["tier"]
            changed = True

    if changed:
        session.commit()

def get_all_candidates() -> list:
    session = get_session(get_engine())
    try:
        _repair_broken_scores(session)
        rows = session.query(Candidate).order_by(Candidate.total_score.desc()).all()
        return [_to_dict(c) for c in rows]
    finally:
        session.close()

def get_candidate(cid: int) -> dict:
    session = get_session(get_engine())
    try:
        _repair_broken_scores(session)
        c = session.get(Candidate, cid)
        if not c:
            return {}
        d = _to_dict(c)
        d["interactions"] = [
            {"direction": i.direction, "subject": i.subject, "body": i.body,
             "round": i.round_number, "timestamp": str(i.timestamp)}
            for i in sorted(c.interactions, key=lambda x: x.id)
        ]
        d["anti_cheat_logs"] = [
            {"check_type": l.check_type, "score": l.similarity_score,
             "details": l.details, "timestamp": str(l.timestamp)}
            for l in sorted(c.anti_cheat_logs, key=lambda x: x.id, reverse=True)
        ]
        d["review_notes"] = [
            {"review_type": r.review_type, "score": r.score,
             "summary": r.summary, "timestamp": str(r.created_at)}
            for r in sorted(c.review_notes, key=lambda x: x.id, reverse=True)
        ]
        d["current_active_weights"] = get_scoring_weights(session)
        return d
    finally:
        session.close()

def get_learnings() -> list:
    session = get_session(get_engine())
    try:
        rows = session.query(SystemLearning).order_by(SystemLearning.generated_at.desc()).limit(10).all()
        return [
            {"generated_at": str(r.generated_at), "candidate_count": r.candidate_count,
             "insights": r.insights or [], "pattern_updates": r.pattern_updates or {}}
            for r in rows
        ]
    finally:
        session.close()

def get_stats() -> dict:
    session = get_session(get_engine())
    try:
        rows  = session.query(Candidate).all()
        total = len(rows)
        tiers = {}
        ai_flags = eliminated = score_sum = 0
        for c in rows:
            tiers[c.tier or "Unknown"] = tiers.get(c.tier or "Unknown", 0) + 1
            ai_flags   += c.ai_flag_count or 0
            eliminated += 1 if c.is_eliminated else 0
            score_sum  += c.total_score or 0
        latest_learning = (
            session.query(SystemLearning)
            .order_by(SystemLearning.generated_at.desc())
            .first()
        )
        latest_learning_count = latest_learning.candidate_count if latest_learning else 0
        return {
            "total_candidates":   total,
            "tier_breakdown":     tiers,
            "ai_flags_total":     ai_flags,
            "eliminated_count":   eliminated,
            "average_score":      round(score_sum / total, 1) if total else 0,
            "interactions_total": session.query(Interaction).count(),
            "learning_status": {
                "candidate_count": total,
                "latest_learning_count": latest_learning_count,
                "learning_due": bool(should_run_analysis(total) and total > latest_learning_count),
                "current_weights": get_scoring_weights(session),
                "last_generated_at": str(latest_learning.generated_at) if latest_learning else "",
            },
        }
    finally:
        session.close()

def _to_dict(c: Candidate) -> dict:
    return {
        "id": c.id, "name": c.name, "email": c.email, "phone": c.phone,
        "college": c.college, "github_url": c.github_url, "skills": c.skills,
        "total_score": c.total_score, "tier": c.tier,
        "score_breakdown": c.score_breakdown or {},
        "total_strikes": c.total_strikes, "is_eliminated": c.is_eliminated,
        "current_round": c.current_round, "ai_flag_count": c.ai_flag_count,
        "copy_flag_count": c.copy_flag_count, "created_at": str(c.created_at),
        "answers": c.answers or {},
    }
