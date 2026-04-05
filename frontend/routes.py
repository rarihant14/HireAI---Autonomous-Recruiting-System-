"""
frontend/routes.py — Frontend Flask Routes
Serves the single-page application and its static assets.
Kept separate from backend/api.py so frontend concerns don't mix
with API logic. This file owns everything the browser directly navigates to.
"""

from flask import Blueprint, render_template, send_from_directory
import os

# Blueprint name "frontend" with no URL prefix — serves from "/"
frontend_bp = Blueprint(
    "frontend",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/static",
)


@frontend_bp.route("/")
def index():
    """Serve the main SPA shell."""
    return render_template("index.html")


@frontend_bp.route("/favicon.ico")
def favicon():
    """Serve favicon (returns 204 if missing rather than 404-spamming logs)."""
    favicon_path = os.path.join(frontend_bp.static_folder, "favicon.ico")
    if os.path.exists(favicon_path):
        return send_from_directory(frontend_bp.static_folder, "favicon.ico")
    return "", 204


@frontend_bp.route("/<path:path>")
def catch_all(path):
    """
    SPA catch-all: any unmatched route returns index.html.
    The JavaScript router handles client-side navigation.
    This also prevents 404 errors when the user refreshes on a deep route.
    """
    return render_template("index.html")
