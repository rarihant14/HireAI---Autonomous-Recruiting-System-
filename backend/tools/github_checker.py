"""
backend/tools/github_checker.py — GitHub Profile Quality Checker
Returns a 0-100 score. Never raises — always returns a safe default on errors.
"""

import re
import requests
from backend.utils import log

GITHUB_API = "https://api.github.com"
HEADERS    = {"Accept": "application/vnd.github.v3+json"}
TIMEOUT    = 5

def extract_username(url: str):
    if not url or not url.strip():
        return None
    url = url.strip().rstrip("/")
    match = re.search(r"github\.com/([A-Za-z0-9\-]+)", url, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.match(r"@([A-Za-z0-9\-]+)$", url)
    return match.group(1) if match else None

def check_github(url: str) -> dict:
    """Score a GitHub profile 0–100. Always returns dict, never raises."""
    base = {"username": "", "exists": False, "score": 0.0,
            "public_repos": 0, "original_repos": 0, "total_stars": 0,
            "followers": 0, "has_bio": False, "account_age_days": -1,
            "notes": [], "error": None}

    username = extract_username(url)
    if not username:
        base["notes"].append(f"No valid GitHub URL ('{url}') → 0/100")
        return base

    base["username"] = username

    try:
        r = requests.get(f"{GITHUB_API}/users/{username}",
                         headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 404:
            base["notes"].append(f"GitHub user '{username}' not found → 0/100")
            return base
        if r.status_code == 403:
            # Rate limited — give neutral score
            base["score"] = 20.0
            base["notes"].append("GitHub API rate-limited → 20/100 default")
            return base
        if r.status_code != 200:
            base["score"] = 15.0
            base["notes"].append(f"GitHub API {r.status_code} → 15/100 default")
            return base

        profile = r.json()
        base["exists"]       = True
        base["public_repos"] = profile.get("public_repos", 0)
        base["followers"]    = profile.get("followers", 0)
        base["has_bio"]      = bool(profile.get("bio"))

        # Fetch repos
        rr = requests.get(f"{GITHUB_API}/users/{username}/repos?per_page=30&sort=updated",
                          headers=HEADERS, timeout=TIMEOUT)
        repos = rr.json() if rr.status_code == 200 and isinstance(rr.json(), list) else []

        original   = [r for r in repos if not r.get("fork") and r.get("description")]
        total_stars = sum(r.get("stargazers_count", 0) for r in repos)
        base["original_repos"] = len(original)
        base["total_stars"]    = total_stars

        score  = 0
        score += min(40, base["public_repos"] * 4)
        score += min(20, len(original) * 5)
        score += min(20, total_stars * 2)
        score += min(10, base["followers"])
        score += 10 if base["has_bio"] else 0

        base["score"] = round(min(100.0, float(score)), 1)
        base["notes"].append(
            f"@{username}: {base['public_repos']} repos, "
            f"{len(original)} original, {total_stars} stars → {base['score']}/100"
        )

    except requests.exceptions.Timeout:
        base["score"] = 15.0
        base["notes"].append(f"GitHub timeout for '{username}' → 15/100")
    except requests.exceptions.ConnectionError:
        base["score"] = 15.0
        base["notes"].append(f"GitHub connection error for '{username}' → 15/100")
    except Exception as e:
        base["score"] = 10.0
        base["notes"].append(f"GitHub error ({e}) → 10/100")
        base["error"] = str(e)

    return base
