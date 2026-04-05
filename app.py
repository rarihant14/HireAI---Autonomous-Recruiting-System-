"""
app.py — Entry Point for the Autonomous Hiring System
Run: python app.py
  → Loads config from .env  (backend/config.py)
  → Initialises database    (backend/database.py)
  → Registers API blueprint (backend/api.py)
  → Registers frontend SPA  (frontend/routes.py)
  → Starts Flask server
  → Opens browser automatically
"""

import time
import threading
import webbrowser

from backend.config import cfg

from flask import Flask, request, jsonify
from flask_cors import CORS


# ── Flask app factory

def create_app() -> Flask:
    # Disable Flask's default root-level static handler so the frontend
    # blueprint can serve assets from frontend/static at /static/...
    app = Flask(__name__, static_folder=None)
    CORS(app)

    # ── Database init
    from backend.database import init_db
    init_db()
    print("[DB] Tables initialised.")

    # ── Backend API blueprint
    from backend.api import api as api_blueprint
    app.register_blueprint(api_blueprint)

    # ── Inline anti-cheat endpoint (keeps api.py focused on CRUD) ─────────
    from backend.agents.anti_cheat_agent import check_candidate_response

    @app.route("/api/anticheat/check", methods=["POST"])
    def anticheat_check():
        data     = request.get_json(silent=True) or {}
        question = data.get("question", "")
        answer   = data.get("answer", "")
        latency  = int(data.get("latency", -1))
        if not question or not answer:
            return jsonify({"error": "question and answer are required"}), 400
        result = check_candidate_response(
            candidate_id  = 0,
            question      = question,
            answer        = answer,
            reply_latency = latency,
        )
        return jsonify(result)

    # Frontend 
    from frontend.routes import frontend_bp
    app.register_blueprint(frontend_bp)

    return app


#Browser auto-open

def _open_browser(host: str, port: int, delay: float = 1.5) -> None:
    """Wait for Flask to be ready, then open the default browser."""
    time.sleep(delay)
    url = f"http://{host}:{port}"
    print(f"[Browser] Opening {url}")
    webbrowser.open(url)


# Main

if __name__ == "__main__":
    # Print startup banner
    print("""HireAI — Autonomous Hiring          
 """)

    # Validate config and print warnings
    for warning in cfg.validate():
        print(f"⚠️  {warning}")

    app = create_app()

    # Open browser in a daemon thread (won't block shutdown)
    threading.Thread(
        target=_open_browser,
        args=(cfg.FLASK_HOST, cfg.FLASK_PORT),
        daemon=True,
    ).start()

    print(f"[Flask] Starting on http://{cfg.FLASK_HOST}:{cfg.FLASK_PORT}")
    app.run(
        host       = cfg.FLASK_HOST,
        port       = cfg.FLASK_PORT,
        debug      = cfg.FLASK_DEBUG,
        use_reloader = False,   # reloader would spawn a second browser tab
    )
