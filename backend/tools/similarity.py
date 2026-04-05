"""
backend/tools/similarity.py — Text Similarity Engine
All cosine similarity, structural comparison, and cross-candidate
copy-ring detection lives here.

Used by:
  - anti_cheat_agent.py (AI-vs-human and cross-candidate checks)
  - learning_agent.py   (finding similar answer patterns)

Keeping this separate means we can swap TF-IDF for sentence-transformers
later without touching any agent code.
"""

import re
import numpy as np
from typing import Optional

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity as sklearn_cosine

from backend.utils import normalise_whitespace, log


# ── Low-level similarity functions ────────────────────────────────────────────

def tfidf_cosine(text_a: str, text_b: str, ngram_range: tuple = (1, 2)) -> float:
    """
    Compute TF-IDF cosine similarity between two texts.
    Returns 0.0–1.0 (1.0 = identical).
    Uses bigrams by default for better phrase-level matching.
    """
    a = normalise_whitespace(text_a)
    b = normalise_whitespace(text_b)

    if not a or not b:
        return 0.0

    try:
        vec    = TfidfVectorizer(ngram_range=ngram_range, min_df=1).fit_transform([a, b])
        score  = float(sklearn_cosine(vec[0], vec[1])[0][0])
        return round(score, 4)
    except Exception as e:
        log("Similarity", f"TF-IDF error: {e}", "WARN")
        return 0.0


def structural_similarity(text_a: str, text_b: str) -> float:
    """
    Compare the writing *structure* of two texts:
      - paragraph count
      - sentence count
      - average sentence length (words)
      - average word length (chars)

    Returns 0.0–1.0 (1.0 = identical structure).
    Does NOT look at vocabulary — purely about shape/rhythm.
    """
    def _features(t: str) -> np.ndarray:
        paras = [p.strip() for p in t.split("\n\n") if p.strip()]
        sents = [s.strip() for s in re.split(r"[.!?]+", t) if s.strip()]
        words = t.split()
        avg_sent_len = np.mean([len(s.split()) for s in sents]) if sents else 0
        avg_word_len = np.mean([len(w) for w in words])         if words else 0
        return np.array([len(paras), len(sents), avg_sent_len, avg_word_len])

    fa = _features(text_a)
    fb = _features(text_b)

    if fa.sum() == 0 or fb.sum() == 0:
        return 0.0

    # Normalised absolute difference per feature, then invert
    diffs = np.abs(fa - fb) / (np.maximum(fa, fb) + 1e-9)
    return round(float(1.0 - np.mean(diffs)), 4)


def phrase_overlap(text_a: str, text_b: str, min_phrase_len: int = 4) -> float:
    """
    Count shared n-word phrases (n >= min_phrase_len) as a fraction of
    shorter text's phrases.  Catches copy-paste that survives paraphrasing.
    Returns 0.0–1.0.
    """
    def _ngrams(text: str, n: int) -> set:
        words = text.lower().split()
        return {" ".join(words[i:i+n]) for i in range(len(words)-n+1)}

    shared = 0
    total  = 0
    for n in range(min_phrase_len, min_phrase_len + 3):   # check 4-, 5-, 6-grams
        ga = _ngrams(text_a, n)
        gb = _ngrams(text_b, n)
        shared += len(ga & gb)
        total  += max(len(ga), len(gb), 1)

    return round(shared / total, 4)


# ── Combined AI-detection score ───────────────────────────────────────────────

def ai_detection_score(
    candidate_answer: str,
    baseline_answer:  str,
    weights: tuple = (0.55, 0.25, 0.20),   # tfidf, structural, phrase
) -> dict:
    """
    Compute a weighted AI-generation likelihood score by comparing a
    candidate's answer against a fresh LLM-generated baseline answer.

    Args:
        candidate_answer  — what the candidate wrote
        baseline_answer   — what an LLM generated for the same question
        weights           — (tfidf_w, structural_w, phrase_w), must sum to 1

    Returns:
        {
          "combined":    float,   # 0–1, overall likelihood of AI generation
          "tfidf":       float,
          "structural":  float,
          "phrase":      float,
          "explanation": str,
        }
    """
    tfidf  = tfidf_cosine(candidate_answer, baseline_answer)
    struct = structural_similarity(candidate_answer, baseline_answer)
    phrase = phrase_overlap(candidate_answer, baseline_answer)

    combined = round(
        tfidf * weights[0] + struct * weights[1] + phrase * weights[2],
        4
    )

    # Build explanation
    signals = []
    if tfidf  > 0.70: signals.append(f"vocabulary overlap ({tfidf:.0%})")
    if struct > 0.75: signals.append(f"identical structure ({struct:.0%})")
    if phrase > 0.30: signals.append(f"shared phrases ({phrase:.0%})")

    if signals:
        explanation = f"Likely AI-generated — {', '.join(signals)}."
    elif combined < 0.40:
        explanation = "Likely human-written — low similarity to LLM baseline."
    else:
        explanation = f"Borderline ({combined:.0%}) — manual review recommended."

    return {
        "combined":    combined,
        "tfidf":       tfidf,
        "structural":  struct,
        "phrase":      phrase,
        "explanation": explanation,
    }


# ── Cross-candidate copy detection ────────────────────────────────────────────

def find_copy_rings(
    candidates: list[dict],
    threshold: float = 0.60,
) -> list[dict]:
    """
    O(n²) pairwise comparison to detect answer-sharing rings.

    Args:
        candidates — list of {"id": int, "answer": str}
        threshold  — cosine similarity above which a pair is flagged

    Returns:
        list of {
          "candidate_a": int,
          "candidate_b": int,
          "similarity":  float,
          "flag":        "COPY_RING",
        }
    """
    detections = []
    n = len(candidates)

    for i in range(n):
        for j in range(i + 1, n):
            a_text = candidates[i].get("answer", "")
            b_text = candidates[j].get("answer", "")

            sim = tfidf_cosine(a_text, b_text)
            if sim >= threshold:
                detections.append({
                    "candidate_a": candidates[i]["id"],
                    "candidate_b": candidates[j]["id"],
                    "similarity":  round(sim, 3),
                    "flag":        "COPY_RING",
                })
                log(
                    "Similarity",
                    f"Copy ring: candidates {candidates[i]['id']} & {candidates[j]['id']} "
                    f"({sim:.0%} similar)"
                )

    return detections
