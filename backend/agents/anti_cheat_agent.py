"""
agents/anti_cheat_agent.py — Component 4: Anti-Cheat
Uses LangGraph + Groq to detect:
  1. AI-generated responses (vs a fresh LLM baseline)
  2. Cross-candidate copying (cosine similarity on TF-IDF)
  3. Suspicious reply timing
"""

import os
import re
import json
import math
import time
from typing import TypedDict, List
try:
    from langgraph.graph import StateGraph, END
    LANGGRAPH_IMPORT_ERROR = ""
except Exception as e:
    StateGraph = None
    END = None
    LANGGRAPH_IMPORT_ERROR = str(e)
try:
    from langchain_groq import ChatGroq
    from langchain.schema import HumanMessage, SystemMessage
    LLM_IMPORT_ERROR = ""
except Exception as e:
    ChatGroq = None
    HumanMessage = None
    SystemMessage = None
    LLM_IMPORT_ERROR = str(e)
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np

# LLM 

def get_llm():
    if ChatGroq is None:
        return None
    return ChatGroq(
        api_key=os.getenv("GROQ_API_KEY"),
        model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
        temperature=0.0,       # deterministic for comparisons
        max_tokens=1024,
    )

AI_THRESHOLD     = float(os.getenv("AI_SIMILARITY_THRESHOLD",  "0.80"))
COPY_THRESHOLD   = float(os.getenv("COPY_SIMILARITY_THRESHOLD","0.60"))
SUSPICIOUS_SECS  = int(os.getenv("SUSPICIOUS_REPLY_SECONDS",   "120"))
AI_FINGERPRINTS = [
    "i'd be happy to help", "i would be happy to", "here's a comprehensive overview",
    "in today's rapidly evolving", "in today's fast-paced", "certainly! here",
    "absolutely! here", "great question", "as an ai language model",
    "it's worth noting that", "it is worth noting", "delve into",
    "leverage", "utilize", "multifaceted", "nuanced", "at the end of the day",
    "in conclusion,", "to summarize,", "this comprehensive",
]

# State 

class AntiCheatState(TypedDict):
    candidate_id:   int
    question:       str
    answer:         str
    reply_latency:  int          # seconds; -1 if unknown
    ai_score:       float        # 0–1
    ai_flagged:     bool
    timing_flagged: bool
    ai_explanation: str
    strikes:        int
    flags:          List[str]

# Helpers 

def _tfidf_similarity(text_a: str, text_b: str) -> float:
    """Cosine similarity between two texts using TF-IDF vectors."""
    if not text_a.strip() or not text_b.strip():
        return 0.0
    try:
        vec = TfidfVectorizer(ngram_range=(1, 2)).fit_transform([text_a, text_b])
        return float(cosine_similarity(vec[0], vec[1])[0][0])
    except Exception:
        return 0.0


def _structural_similarity(text_a: str, text_b: str) -> float:
    """
    Lightweight structural similarity:
    Compare sentence-count, paragraph-count, and avg-sentence-length.
    Returns 0–1 where 1 = identical structure.
    """
    def features(t):
        paras = [p.strip() for p in t.split("\n\n") if p.strip()]
        sents = re.split(r"[.!?]+", t)
        sents = [s.strip() for s in sents if s.strip()]
        avg_len = np.mean([len(s.split()) for s in sents]) if sents else 0
        return (len(paras), len(sents), avg_len)

    fa, fb = features(text_a), features(text_b)
    if fa == (0,0,0) or fb == (0,0,0):
        return 0.0

    diffs = [abs(a - b) / (max(a, b) + 1e-9) for a, b in zip(fa, fb)]
    return round(1 - sum(diffs) / len(diffs), 3)

#Nodes

def check_ai_generated(state: AntiCheatState) -> AntiCheatState:
    """
    Compare candidate answer against a fresh LLM-generated answer to the same question.
    Combines TF-IDF cosine similarity + structural similarity.
    """
    question = state["question"]
    answer   = state["answer"]
    flags    = state["flags"]

    llm = get_llm()
    if llm is None:
        answer_lower = answer.lower()
        hits = [phrase for phrase in AI_FINGERPRINTS if phrase in answer_lower]
        heuristic_score = min(0.95, round(0.18 * len(hits), 3))
        state["ai_score"] = heuristic_score
        state["ai_flagged"] = heuristic_score >= AI_THRESHOLD or len(hits) >= 4
        if state["ai_flagged"]:
            state["strikes"] += 1
            flags.append(f"AI_FINGERPRINT:{heuristic_score:.2f}")
            state["ai_explanation"] = (
                f"Likely AI-written based on repeated AI-style phrases"
                f" ({', '.join(hits[:3])}) while the LLM baseline check is unavailable."
            )
        else:
            state["ai_explanation"] = (
                f"AI baseline check fell back to phrase heuristics and found no strong AI signal"
                f"{': ' + LLM_IMPORT_ERROR if LLM_IMPORT_ERROR else ''}"
            )
        state["flags"] = flags
        return state
    # Generate a baseline AI answer to the same question
    try:
        baseline_resp = llm.invoke([
            SystemMessage(content=(
                "You are a typical fresh-graduate job applicant answering screening questions. "
                "Write a natural, moderately polished answer in 2–4 paragraphs."
            )),
            HumanMessage(content=f"Question: {question}\n\nWrite your answer:"),
        ])
        baseline = baseline_resp.content.strip()
    except Exception as e:
        state["ai_score"]      = 0.0
        state["ai_flagged"]    = False
        state["ai_explanation"]= f"Could not generate baseline (error: {e})"
        return state

    # Compute similarities
    tfidf_sim  = _tfidf_similarity(answer, baseline)
    struct_sim = _structural_similarity(answer, baseline)
    combined   = round(0.65 * tfidf_sim + 0.35 * struct_sim, 3)

    state["ai_score"] = combined

    if combined >= AI_THRESHOLD:
        state["ai_flagged"] = True
        state["strikes"]   += 1
        explanation = (
            f"AI-generated (score {combined:.0%}): "
            f"TF-IDF={tfidf_sim:.0%}, structure={struct_sim:.0%}. "
            "Response closely mirrors a fresh LLM output in wording and structure."
        )
        flags.append(f"AI_GENERATED:{combined:.2f}")
    else:
        state["ai_flagged"]    = False
        explanation = f"Likely human (score {combined:.0%})"

    state["ai_explanation"] = explanation
    state["flags"]          = flags
    return state


def check_timing(state: AntiCheatState) -> AntiCheatState:
    """Flag replies that arrived suspiciously fast."""
    latency = state["reply_latency"]
    flags   = state["flags"]

    if latency != -1 and latency < SUSPICIOUS_SECS:
        state["timing_flagged"] = True
        state["strikes"]       += 1
        flags.append(f"FAST_REPLY:{latency}s")
    else:
        state["timing_flagged"] = False

    state["flags"] = flags
    return state


# Graph

def build_anti_cheat_graph():
    if StateGraph is None:
        raise RuntimeError(f"LangGraph unavailable: {LANGGRAPH_IMPORT_ERROR}")
    g = StateGraph(AntiCheatState)
    g.add_node("ai_check",     check_ai_generated)
    g.add_node("timing_check", check_timing)
    g.set_entry_point("ai_check")
    g.add_edge("ai_check",     "timing_check")
    g.add_edge("timing_check", END)
    return g.compile()


_graph = None

def _run_anti_cheat_fallback(initial: AntiCheatState) -> AntiCheatState:
    state = check_ai_generated(initial)
    state = check_timing(state)
    return state

def check_candidate_response(
    candidate_id: int,
    question: str,
    answer: str,
    reply_latency: int = -1,
) -> dict:
    """
    Run all anti-cheat checks for a single candidate response.
    Returns a dict with flags, strikes, scores, and explanations.
    """
    global _graph

    initial: AntiCheatState = {
        "candidate_id":  candidate_id,
        "question":      question,
        "answer":        answer,
        "reply_latency": reply_latency,
        "ai_score":      0.0,
        "ai_flagged":    False,
        "timing_flagged":False,
        "ai_explanation":"",
        "strikes":       0,
        "flags":         [],
    }
    if StateGraph is None:
        result = _run_anti_cheat_fallback(initial)
    else:
        try:
            if _graph is None:
                _graph = build_anti_cheat_graph()
            result = _graph.invoke(initial)
        except Exception as e:
            if "LangGraph" in str(e) or "pydantic_core" in str(e):
                result = _run_anti_cheat_fallback(initial)
            else:
                raise
    return {
        "candidate_id":  result["candidate_id"],
        "ai_score":      result["ai_score"],
        "ai_flagged":    result["ai_flagged"],
        "timing_flagged":result["timing_flagged"],
        "ai_explanation":result["ai_explanation"],
        "strikes":       result["strikes"],
        "flags":         result["flags"],
    }


def cross_candidate_check(candidates: list) -> list:
    """
    Compare every candidate's answers against each other (O(n²)).
    Returns list of dicts describing copy-ring detections.

    candidates: list of {"id": int, "answer": str}
    """
    detections = []
    n = len(candidates)
    if n < 2:
        return detections

    for i in range(n):
        for j in range(i + 1, n):
            sim = _tfidf_similarity(candidates[i]["answer"], candidates[j]["answer"])
            if sim >= COPY_THRESHOLD:
                detections.append({
                    "candidate_a": candidates[i]["id"],
                    "candidate_b": candidates[j]["id"],
                    "similarity":  round(sim, 3),
                    "flag":        "COPY_RING",
                })
    return detections
