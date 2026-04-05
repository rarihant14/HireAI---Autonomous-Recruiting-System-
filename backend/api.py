"""
backend/api.py — Flask REST API + SocketIO
All endpoints the frontend calls.
"""

import os
import json
import tempfile
from flask import Blueprint, request, jsonify
from werkzeug.utils import secure_filename
from backend.orchestrator import (
    ingest_batch, get_all_candidates, get_candidate,
    get_learnings, get_learning_status, get_stats, start_email_polling,
    stop_email_polling, run_learning_now,
)
from backend.queue import celery_app, queue_enabled
from backend.tools.data_extractor import (
    parse_csv_upload, parse_excel_upload, generate_demo_candidates
)

api = Blueprint("api", __name__, url_prefix="/api")

ALLOWED_EXTENSIONS = {"csv", "xlsx", "xls"}

def _allowed(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _queue_response(task, accepted_message: str):
    return jsonify({
        "status": "queued",
        "message": accepted_message,
        "task_id": task.id,
    }), 202

# ── Candidates 

@api.route("/candidates", methods=["GET"])
def list_candidates():
    tier   = request.args.get("tier")
    search = request.args.get("search", "").lower()
    rows   = get_all_candidates()
    if tier:
        rows = [r for r in rows if r.get("tier") == tier]
    if search:
        rows = [r for r in rows if search in r.get("name","").lower() or search in r.get("email","").lower()]
    return jsonify(rows)


@api.route("/candidates/<int:cid>", methods=["GET"])
def get_one_candidate(cid):
    c = get_candidate(cid)
    if not c:
        return jsonify({"error": "Not found"}), 404
    return jsonify(c)


@api.route("/stats", methods=["GET"])
def stats():
    return jsonify(get_stats())


@api.route("/learnings", methods=["GET"])
def learnings():
    return jsonify(get_learnings())


@api.route("/learning/status", methods=["GET"])
def learning_status():
    return jsonify(get_learning_status())

# ── Ingestion 

@api.route("/ingest/demo", methods=["POST"])
def ingest_demo():
    """Ingest N synthetic demo candidates for testing."""
    n        = int(request.json.get("count", 20))
    job_role = request.json.get("job_role", "Software Engineer Intern")
    n        = min(n, 100)  # safety cap
    candidates = generate_demo_candidates(n)

    if queue_enabled() and celery_app is not None:
        task = celery_app.send_task("hireai.ingest_batch", args=[candidates, job_role])
        return _queue_response(task, "Demo ingestion queued")

    results    = ingest_batch(candidates, job_role)
    ok  = sum(1 for r in results if r["status"] in ("ingested", "duplicate"))
    return jsonify({"ingested": ok, "total": len(results), "details": results[:5]})


@api.route("/ingest/upload", methods=["POST"])
def ingest_upload():
    """Upload a CSV or XLSX file of applicants."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not _allowed(f.filename):
        return jsonify({"error": "Only CSV and XLSX files supported"}), 400

    job_role = request.form.get("job_role", "Software Engineer Intern")
    suffix   = "." + f.filename.rsplit(".", 1)[1].lower()

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        f.save(tmp.name)
        try:
            if suffix == ".csv":
                candidates = parse_csv_upload(tmp.name)
            else:
                candidates = parse_excel_upload(tmp.name)
        except Exception as e:
            return jsonify({"error": str(e)}), 422

    if queue_enabled() and celery_app is not None:
        task = celery_app.send_task("hireai.ingest_batch", args=[candidates, job_role])
        return _queue_response(task, "Upload ingestion queued")

    results = ingest_batch(candidates, job_role)
    ok = sum(1 for r in results if r["status"] in ("ingested", "duplicate"))
    return jsonify({"ingested": ok, "total": len(results)})

# ── Email polling control 

@api.route("/polling/start", methods=["POST"])
def polling_start():
    return jsonify(start_email_polling())


@api.route("/polling/stop", methods=["POST"])
def polling_stop():
    return jsonify(stop_email_polling())

# ── Manual learning trigger 

@api.route("/learning/run", methods=["POST"])
def run_learning():
    if queue_enabled() and celery_app is not None:
        task = celery_app.send_task("hireai.run_learning")
        return _queue_response(task, "Learning cycle queued")

    latest = run_learning_now()
    return jsonify({
        "status": "learning cycle completed",
        "latest": latest,
    })


@api.route("/tasks/<task_id>", methods=["GET"])
def task_status(task_id):
    if not queue_enabled() or celery_app is None:
        return jsonify({"error": "Task queue is not configured"}), 404

    result = celery_app.AsyncResult(task_id)
    payload = {
        "task_id": task_id,
        "state": result.state,
        "ready": result.ready(),
        "successful": result.successful() if result.ready() else False,
    }
    if result.ready():
        payload["result"] = result.result
    return jsonify(payload)
