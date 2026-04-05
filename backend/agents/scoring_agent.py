"""
backend/agents/scoring_agent.py — Component 2: Intelligence
Scores and ranks applicants using LangGraph + Groq.
Falls back to heuristic scoring if Groq is unavailable.

Graph: parse → skills → answers → github → penalties → completeness → final → tier
"""

import re
import json
import requests
from typing import TypedDict, Any
try:
    from langgraph.graph import StateGraph, END
    LANGGRAPH_IMPORT_ERROR = ""
except Exception as e:
    StateGraph = None
    END = None
    LANGGRAPH_IMPORT_ERROR = str(e)

from backend.config import cfg
from backend.database import get_engine, get_scoring_weights, get_session
from backend.utils  import log, safe_json_loads, word_count

# AI fingerprint phrases 
AI_FINGERPRINTS = [
    "i'd be happy to help", "i would be happy to", "here's a comprehensive overview",
    "in today's rapidly evolving", "in today's fast-paced", "certainly! here",
    "absolutely! here", "great question", "as an ai language model",
    "it's worth noting that", "it is worth noting", "delve into",
    "leverage", "utilize", "multifaceted", "nuanced", "at the end of the day",
    "in conclusion,", "to summarize,", "this comprehensive",
]

# State
class ScoringState(TypedDict):
    candidate:          dict
    skills_score:       float
    answer_score:       float
    github_score:       float
    penalty_score:      float
    completeness_score: float
    final_score:        float
    tier:               str
    breakdown:          dict
    notes:              list
    weights_used:       dict

# ── LLM (optional) 
def _try_get_llm():
    """Return ChatGroq instance or None if not configured."""
    try:
        if not cfg.GROQ_API_KEY or cfg.GROQ_API_KEY == "your_groq_api_key_here":
            return None
        from langchain_groq import ChatGroq
        return ChatGroq(
            api_key     = cfg.GROQ_API_KEY,
            model       = cfg.GROQ_MODEL,
            temperature = cfg.GROQ_TEMPERATURE,
            max_tokens  = 1024,
        )
    except Exception:
        return None

# ── Nodes

def parse_candidate(state: ScoringState) -> ScoringState:
    """Normalise raw candidate data — handle missing fields gracefully."""
    c = state["candidate"]
    c.setdefault("skills", "")
    c.setdefault("answers", {})
    c.setdefault("github_url", "")
    c.setdefault("name", "Unknown")
    c.setdefault("email", "")

    if isinstance(c["skills"], str):
        c["skills_list"] = [s.strip().lower() for s in c["skills"].split(",") if s.strip()]
    elif isinstance(c["skills"], list):
        c["skills_list"] = [s.strip().lower() for s in c["skills"]]
    else:
        c["skills_list"] = []

    state["candidate"] = c
    state["notes"] = []
    state["weights_used"] = _load_dynamic_weights()
    return state


def _load_dynamic_weights() -> dict:
    """Fetch current scoring weights from the database, with safe defaults."""
    session = None
    defaults = {
        "technical_skills": 0.25,
        "answer_quality": 0.25,
        "github_quality": 0.20,
        "ai_penalty": 0.15,
        "completeness": 0.15,
    }
    try:
        session = get_session(get_engine(cfg.DATABASE_URL))
        weights = get_scoring_weights(session)
        total = sum(weights.values())
        if total <= 0:
            return defaults
        return {k: round(v / total, 4) for k, v in weights.items()}
    except Exception as e:
        log("Scoring", f"Dynamic weight load failed, using defaults: {e}", "WARN")
        return defaults
    finally:
        if session:
            session.close()


def score_skills(state: ScoringState) -> ScoringState:
    """Score technical skills (0–100). No LLM needed."""
    skills = state["candidate"].get("skills_list", [])

    HIGH_VALUE = {
        "python", "langchain", "langgraph", "fastapi", "django", "docker",
        "kubernetes", "postgresql", "mongodb", "redis", "aws", "gcp",
        "machine learning", "nlp", "pytorch", "tensorflow", "react",
        "typescript", "rust", "golang", "scrapy", "selenium", "playwright",
        "flask", "node.js", "spring boot", "kafka", "elasticsearch",
    }
    MEDIUM_VALUE = {
        "javascript", "html", "css", "sql", "git", "linux",
        "rest api", "graphql", "java", "c++", "c#", "php",
        "mysql", "sqlite", "express", "vue", "angular",
    }

    skills_text = " ".join(skills)
    high_matches = [h for h in HIGH_VALUE  if h in skills_text]
    med_matches  = [m for m in MEDIUM_VALUE if m in skills_text]

    raw   = min(60, len(high_matches) * 12) + min(30, len(med_matches) * 6)
    bonus = min(10, len(skills)) if len(skills) >= 5 else 0
    score = min(100, raw + bonus)

    state["skills_score"] = float(score)
    state["notes"].append(
        f"Skills: {len(high_matches)} high-value, {len(med_matches)} medium → {score}/100"
    )
    return state


def score_answers(state: ScoringState) -> ScoringState:
    """
    Score answer quality (0–100).
    Uses Groq LLM if available, otherwise heuristic scoring.
    """
    answers = state["candidate"].get("answers", {})
    notes   = state["notes"]

    if not answers:
        state["answer_score"] = 0.0
        notes.append("Answers: none provided → 0/100")
        return state

    answers_text = "\n\n".join(f"Q: {q}\nA: {a}" for q, a in answers.items() if a)
    if not answers_text.strip():
        state["answer_score"] = 0.0
        notes.append("Answers: all blank → 0/100")
        return state

    # Try LLM scoring
    llm = _try_get_llm()
    if llm:
        try:
            from langchain.schema import HumanMessage, SystemMessage
            system = (
                "You are an expert technical recruiter evaluating job application answers. "
                "Score the answers 0–100 based on: specificity, technical depth, originality, relevance. "
                'Return ONLY JSON: {"score": <number>, "reasoning": "<one sentence>"}. No other text.'
            )
            resp = llm.invoke([
                SystemMessage(content=system),
                HumanMessage(content=f"Evaluate these answers:\n\n{answers_text[:2000]}")
            ])
            data  = safe_json_loads(resp.content, fallback={})
            score = float(data.get("score", 50))
            score = min(100, max(0, score))
            state["answer_score"] = score
            notes.append(f"Answer quality (LLM): {score}/100 — {data.get('reasoning','')}")
            return state
        except Exception as e:
            log("Scoring", f"LLM answer scoring failed, using heuristic: {e}", "WARN")

    # Heuristic fallback — always works without Groq
    total_words = sum(word_count(str(a)) for a in answers.values())
    has_specifics = any(
        kw in answers_text.lower()
        for kw in ["github", "http", "api", "database", "docker", "python",
                   "built", "implemented", "deployed", "used", "wrote", "created"]
    )
    # Base score from word count (20 words = 20pts, up to 60)
    score = min(60, total_words * 0.8)
    # Bonus for specific technical mentions
    if has_specifics:
        score += 20
    # Penalty for very short answers
    if total_words < 30:
        score = min(score, 25)

    state["answer_score"] = round(min(100, score), 1)
    notes.append(f"Answer quality (heuristic, {total_words} words): {state['answer_score']}/100")
    return state


def score_github(state: ScoringState) -> ScoringState:
    """Evaluate GitHub profile quality (0–100). No LLM needed."""
    from backend.tools.github_checker import check_github
    url   = state["candidate"].get("github_url", "").strip()
    notes = state["notes"]

    if not url or url.lower() in ("", "n/a", "none", "na", "-"):
        state["github_score"] = 0.0
        notes.append("GitHub: not provided → 0/100")
        return state

    try:
        result = check_github(url)
        state["github_score"] = result["score"]
        notes.extend(result["notes"])
    except Exception as e:
        # If GitHub API fails (rate limit etc.), give neutral score
        state["github_score"] = 20.0
        notes.append(f"GitHub: check failed ({e}) → 20/100 default")

    return state


def apply_penalties(state: ScoringState) -> ScoringState:
    """Deduct penalty points for AI fingerprints and one-word answers."""
    answers = state["candidate"].get("answers", {})
    notes   = state["notes"]
    penalty = 0.0
    reasons = []

    all_text = " ".join(str(v).lower() for v in answers.values())

    # AI fingerprint check
    hits = [p for p in AI_FINGERPRINTS if p in all_text]
    if hits:
        p = min(60, len(hits) * 15)
        penalty += p
        reasons.append(f"AI phrases detected ({len(hits)}: e.g. '{hits[0]}') −{p}pts")

    # Short answer check
    for q, a in answers.items():
        wc = word_count(str(a))
        if wc == 0:
            penalty += 25
            reasons.append(f"Blank answer → −25pts")
        elif wc < 10:
            penalty += 15
            reasons.append(f"Very short ({wc} words) → −15pts")

    state["penalty_score"] = min(100, penalty)
    notes.append("Penalties: " + ("; ".join(reasons) if reasons else "none"))
    return state


def score_completeness(state: ScoringState) -> ScoringState:
    """Score profile completeness (0–100). No LLM needed."""
    c     = state["candidate"]
    score = 0
    if c.get("name")   and c["name"]   not in ("Unknown", ""): score += 10
    if c.get("email")  and "@" in c["email"]:                  score += 20
    if c.get("phone"):                                          score += 10
    if c.get("college"):                                        score += 15
    if c.get("github_url") and c["github_url"] not in ("", "N/A", "n/a"): score += 20
    if c.get("skills") and len(c.get("skills_list", [])) >= 2:            score += 10
    if c.get("answers") and any(v for v in c["answers"].values()):         score += 15

    state["completeness_score"] = float(score)
    state["notes"].append(f"Completeness: {score}/100")
    return state


def compute_final(state: ScoringState) -> ScoringState:
    """Weighted combination of all component scores."""
    learned = state.get("weights_used") or _load_dynamic_weights()
    w = {
        "skills": learned.get("technical_skills", 0.25),
        "answers": learned.get("answer_quality", 0.25),
        "github": learned.get("github_quality", 0.20),
        "penalty": learned.get("ai_penalty", 0.15),
        "complete": learned.get("completeness", 0.15),
    }

    raw = (
        state["skills_score"]       * w["skills"]  +
        state["answer_score"]       * w["answers"] +
        state["github_score"]       * w["github"]  +
        state["completeness_score"] * w["complete"]
    )
    deduction = state["penalty_score"] * w["penalty"]
    final     = round(max(0.0, raw - deduction), 2)

    state["final_score"] = final
    state["breakdown"]   = {
        "skills_score":       state["skills_score"],
        "answer_score":       state["answer_score"],
        "github_score":       state["github_score"],
        "penalty_score":      state["penalty_score"],
        "completeness_score": state["completeness_score"],
        "weights_used": {
            "technical_skills": w["skills"],
            "answer_quality": w["answers"],
            "github_quality": w["github"],
            "ai_penalty": w["penalty"],
            "completeness": w["complete"],
        },
        "final_score":        final,
    }
    state["notes"].append(
        "Weights: "
        f"skills={w['skills']:.2f}, answers={w['answers']:.2f}, github={w['github']:.2f}, "
        f"penalty={w['penalty']:.2f}, completeness={w['complete']:.2f}"
    )
    return state


def assign_tier(state: ScoringState) -> ScoringState:
    """Assign tier label based on final score."""
    s = state["final_score"]
    if   s >= 70: state["tier"] = "Fast-Track"
    elif s >= 50: state["tier"] = "Standard"
    elif s >= 35: state["tier"] = "Review"
    else:         state["tier"] = "Reject"
    state["notes"].append(f"→ Final: {s}/100  Tier: {state['tier']}")
    return state


# Graph 

def build_scoring_graph():
    if StateGraph is None:
        raise RuntimeError(f"LangGraph unavailable: {LANGGRAPH_IMPORT_ERROR}")
    g = StateGraph(ScoringState)
    for name, fn in [
        ("parse_node",        parse_candidate),
        ("skills_node",       score_skills),
        ("answers_node",      score_answers),
        ("github_node",       score_github),
        ("penalties_node",    apply_penalties),
        ("completeness_node", score_completeness),
        ("final_node",        compute_final),
        ("tier_node",         assign_tier),
    ]:
        g.add_node(name, fn)

    g.set_entry_point("parse_node")
    g.add_edge("parse_node",        "skills_node")
    g.add_edge("skills_node",       "answers_node")
    g.add_edge("answers_node",      "github_node")
    g.add_edge("github_node",       "penalties_node")
    g.add_edge("penalties_node",    "completeness_node")
    g.add_edge("completeness_node", "final_node")
    g.add_edge("final_node",        "tier_node")
    g.add_edge("tier_node",         END)
    return g.compile()

_graph = None

def _run_scoring_fallback(initial: ScoringState) -> ScoringState:
    """Sequential fallback when LangGraph is unavailable."""
    state = initial
    for fn in (
        parse_candidate,
        score_skills,
        score_answers,
        score_github,
        apply_penalties,
        score_completeness,
        compute_final,
        assign_tier,
    ):
        state = fn(state)
    if LANGGRAPH_IMPORT_ERROR:
        state["notes"].append(f"LangGraph fallback active: {LANGGRAPH_IMPORT_ERROR}")
    return state

def score_candidate(candidate: dict) -> dict:
    """
    Score a single candidate. Always returns a valid result dict —
    never raises, so ingestion never fails due to scoring errors.
    """
    global _graph

    try:
        initial: ScoringState = {
            "candidate":          candidate,
            "skills_score":       0.0,
            "answer_score":       0.0,
            "github_score":       0.0,
            "penalty_score":      0.0,
            "completeness_score": 0.0,
            "final_score":        0.0,
            "tier":               "Reject",
            "breakdown":          {},
            "notes":              [],
            "weights_used":       {},
        }
        if StateGraph is None:
            return _run_scoring_fallback(initial)
        if _graph is None:
            _graph = build_scoring_graph()
        return _graph.invoke(initial)
    except Exception as e:
        # Nuclear fallback — scoring must never crash ingestion
        log("Scoring", f"Graph error for {candidate.get('email','?')}: {e}", "ERROR")
        return {
            "candidate":          candidate,
            "skills_score":       0.0,
            "answer_score":       0.0,
            "github_score":       0.0,
            "penalty_score":      0.0,
            "completeness_score": 0.0,
            "final_score":        0.0,
            "tier":               "Review",
            "breakdown":          {},
            "notes":              [f"Scoring error: {e}"],
            "weights_used":       {},
        }
