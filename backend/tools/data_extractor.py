"""
backend/tools/data_extractor.py — Component 1: Data Ingestion
Parses CSV/XLSX applicant files and generates demo candidates.
"""

import csv
import random
import uuid
try:
    import pandas as pd
    PANDAS_IMPORT_ERROR = ""
except Exception as e:
    pd = None
    PANDAS_IMPORT_ERROR = str(e)
from backend.utils import log

COLUMN_MAP = {
    "Name": "name", "Email": "email", "Phone": "phone", "College": "college",
    "GitHub Profile": "github_url", "Github": "github_url", "GitHub": "github_url",
    "Resume": "resume_url", "Skills": "skills",
    "Why do you want to apply?": "answer_motivation",
    "Describe a project": "answer_project",
    "Cover Letter": "answer_cover",
    "Score": "platform_score",
}

def _normalise_row(row: dict) -> dict:
    result = {}
    for raw_key, value in row.items():
        key = COLUMN_MAP.get(raw_key.strip(), raw_key.strip().lower().replace(" ", "_"))
        result[key] = str(value).strip() if value else ""

    # Consolidate answer_ fields into answers dict
    answers = {}
    for field in list(result.keys()):
        if field.startswith("answer_"):
            question = field.replace("answer_", "").replace("_", " ").title()
            answers[question] = result.pop(field)
    result["answers"] = answers or {}
    result.setdefault("github_url", "")
    result.setdefault("skills", "")
    result.setdefault("phone", "")
    result.setdefault("college", "")
    return result

def parse_csv_upload(file_path: str) -> list:
    candidates = []
    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                candidates.append(_normalise_row(dict(row)))
        log("Extractor", f"Parsed {len(candidates)} rows from CSV")
    except Exception as e:
        raise RuntimeError(f"CSV parse error: {e}")
    return candidates

def parse_excel_upload(file_path: str) -> list:
    try:
        if pd is None:
            raise RuntimeError(f"pandas is unavailable: {PANDAS_IMPORT_ERROR}")
        df = pd.read_excel(file_path, dtype=str).fillna("")
        candidates = [_normalise_row(row.to_dict()) for _, row in df.iterrows()]
        log("Extractor", f"Parsed {len(candidates)} rows from Excel")
        return candidates
    except Exception as e:
        raise RuntimeError(f"Excel parse error: {e}")

def generate_demo_candidates(n: int = 20) -> list:
    """
    Generate n realistic synthetic candidates for testing.
    Every email is globally unique — no duplicates across multiple calls.
    Mix of strong, average, weak, and AI-cheating profiles.
    """
    colleges = [
        "IIT Bombay", "NIT Nagpur", "BITS Pilani", "VIT Vellore",
        "Pune University", "COEP Pune", "VJTI Mumbai", "MIT Manipal",
        "IIIT Hyderabad", "Delhi University", "Amity University",
    ]
    first_names = ["Rahul", "Priya", "Amit", "Sneha", "Rohan", "Ananya",
                   "Karan", "Divya", "Arjun", "Meera", "Vikram", "Pooja"]
    last_names  = ["Sharma", "Patel", "Singh", "Kumar", "Verma", "Gupta",
                   "Joshi", "Mehta", "Nair", "Rao", "Reddy", "Iyer"]

    skill_pools = {
        "strong":   "Python, FastAPI, LangChain, Docker, PostgreSQL, AWS, React, TypeScript",
        "good":     "Python, Django, MySQL, Git, Linux, REST API, JavaScript",
        "average":  "Python, Flask, SQLite, HTML, CSS, Git",
        "weak":     "HTML, CSS, MS Office",
        "very_weak":"Communication Skills, Teamwork",
    }
    github_urls = {
        "strong":   "https://github.com/octocat",       # real account for testing
        "good":     "https://github.com/torvalds",
        "average":  "https://github.com/demo_avg_user",
        "weak":     "",
        "very_weak":"",
    }
    human_answers = [
        "I built a REST API with FastAPI and PostgreSQL deployed on AWS EC2 with Docker. "
        "Biggest challenge was managing DB connection pools under load — solved with SQLAlchemy async sessions.",
        "Set up CI/CD with GitHub Actions → Docker → Railway. Multi-stage builds cut image size by 60%. "
        "Debugging: py-spy showed JSON serialisation was the bottleneck; switched to orjson.",
        "Scraped 50k products using Scrapy + Splash for JS pages. Hit rate limits on day 2 — "
        "added rotating proxies and exponential backoff. Now runs daily via cron.",
        "Built a LangChain agent with 3 tools: web search, SQLite lookup, calculator. "
        "Hardest part: getting tool-selection prompts right so it didn't hallucinate tool names.",
        "Wrote a real-time dashboard with WebSockets, React, and Redis pub/sub. "
        "Latency under 50ms for 1000 concurrent users.",
    ]
    ai_answers = [
        "I'd be happy to help! In today's rapidly evolving tech landscape, "
        "it's important to leverage cutting-edge technologies. Here's a comprehensive overview "
        "of my approach to this multifaceted challenge.",
        "Certainly! As an enthusiastic developer with a passion for innovation, "
        "I would utilize various frameworks to deliver a nuanced solution. "
        "At the end of the day, what matters is delivering value.",
        "Great question! I strongly believe that in order to succeed in today's fast-paced environment, "
        "one must embrace a holistic approach. In conclusion, I am the ideal candidate.",
    ]
    vague_answers = [
        "I have good experience with programming and I like to learn new things.",
        "I worked on many projects and I am passionate about technology.",
        "I am a quick learner and team player with good communication skills.",
    ]

    results = []
    batch_id = uuid.uuid4().hex[:8]   # unique per call so re-runs don't collide

    for i in range(n):
        # Determine profile type
        pct = i / n
        if   pct < 0.15: profile = "strong"
        elif pct < 0.35: profile = "good"
        elif pct < 0.55: profile = "average"
        elif pct < 0.70: profile = "weak"
        else:            profile = "very_weak"

        # Pick answer style
        if profile in ("strong", "good"):
            answer = random.choice(human_answers)
        elif profile == "average":
            # Mix: some human, some AI, some vague
            answer = random.choice(human_answers + ai_answers + vague_answers)
        else:
            answer = random.choice(ai_answers + vague_answers)

        name  = f"{random.choice(first_names)} {random.choice(last_names)}"
        # Globally unique email — batch_id + index ensures no duplicates
        email = f"candidate_{batch_id}_{i+1:03d}@example.com"

        candidate = {
            "name":       name,
            "email":      email,
            "phone":      f"+91{random.randint(7000000000, 9999999999)}",
            "college":    random.choice(colleges),
            "github_url": github_urls[profile],
            "resume_url": f"https://example.com/resume/{batch_id}_{i}",
            "skills":     skill_pools[profile],
            "answers": {
                "Why do you want to apply?":    answer,
                "Describe a relevant project":  answer if profile in ("strong","good") else "",
            },
            "platform_score": str(random.randint(60, 95) if profile in ("strong","good") else random.randint(10, 55)),
        }
        results.append(candidate)

    log("Extractor", f"Generated {len(results)} demo candidates (batch {batch_id})")
    return results
