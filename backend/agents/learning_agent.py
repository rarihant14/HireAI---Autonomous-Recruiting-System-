"""
agents/learning_agent.py — Component 5: Self-Learning
Analyses accumulated interaction data every N candidates and
updates scoring patterns fed back into the scoring pipeline.
"""

import os
import json
from datetime import datetime
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

ANALYSIS_INTERVAL = 10   # run analysis every N candidates


def get_llm():
    if ChatGroq is None:
        return None
    return ChatGroq(
        api_key=os.getenv("GROQ_API_KEY"),
        model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
        temperature=0.2,
        max_tokens=2048,
    )

# State

class LearningState(TypedDict):
    candidates_data:  List[dict]   # list of candidate summaries
    interactions_data:List[dict]   # list of email exchange summaries
    insights:         List[str]
    pattern_updates:  dict         # suggested weight adjustments
    raw_report:       str
    error:            str


def _format_candidate_label(candidate: dict) -> str:
    name = (candidate.get("name") or "").strip() or "Unknown"
    email = (candidate.get("email") or "").strip()
    return f"{name} ({email})" if email else name


def _build_performance_insights(candidates: List[dict]) -> List[str]:
    if not candidates:
        return ["No candidates available yet to identify best or worst performance."]

    ranked = sorted(candidates, key=lambda c: c.get("final_score", 0), reverse=True)
    best = ranked[0]
    worst = ranked[-1]

    insights = [
        f"Best candidate: {_format_candidate_label(best)} scored {best.get('final_score', 0):.1f} and landed in {best.get('tier', 'Unknown')}.",
        f"Worst performer: {_format_candidate_label(worst)} scored {worst.get('final_score', 0):.1f} and landed in {worst.get('tier', 'Unknown')}.",
    ]

    if len(ranked) > 1:
        gap = round(best.get("final_score", 0) - worst.get("final_score", 0), 1)
        insights.append(f"Performance gap between best and worst candidates is {gap:.1f} points.")

    return insights

# ── Node

def gather_patterns(state: LearningState) -> LearningState:
    """
    Summarise the dataset into a compact text the LLM can reason over.
    This node does the data-crunching before the LLM call.
    """
    candidates   = state["candidates_data"]
    interactions = state["interactions_data"]

    # Compute quick stats
    total         = len(candidates)
    tiers         = {}
    avg_scores    = {}
    ai_flag_count = 0
    copy_flag_count = 0

    for c in candidates:
        tier = c.get("tier", "Unknown")
        tiers[tier] = tiers.get(tier, 0) + 1
        for key in ("skills_score","answer_score","github_score","final_score"):
            avg_scores.setdefault(key, []).append(c.get(key, 0))
        ai_flag_count   += c.get("ai_flag_count", 0)
        copy_flag_count += c.get("copy_flag_count", 0)

    avg = {k: round(sum(v)/len(v), 1) for k, v in avg_scores.items() if v}

    # Collect top-performing candidate answers as positive examples
    top_answers = []
    sorted_cands = sorted(candidates, key=lambda c: c.get("final_score", 0), reverse=True)
    for c in sorted_cands[:5]:
        for q, a in (c.get("answers") or {}).items():
            top_answers.append(f"[Score:{c['final_score']}] Q: {q[:80]}\nA: {str(a)[:200]}")

    performance_insights = _build_performance_insights(candidates)

    summary = (
        f"DATASET SUMMARY ({total} candidates)\n"
        f"Tier distribution: {json.dumps(tiers)}\n"
        f"Average scores: {json.dumps(avg)}\n"
        f"AI-generation flags: {ai_flag_count}\n"
        f"Copy-ring flags: {copy_flag_count}\n"
        f"PERFORMANCE HIGHLIGHTS: {json.dumps(performance_insights)}\n\n"
        f"TOP PERFORMER ANSWERS:\n" + "\n---\n".join(top_answers[:10])
    )

    # Attach summary to state for the LLM node
    state["raw_report"] = summary
    return state


def generate_insights(state: LearningState) -> LearningState:
    """Ask the LLM to derive actionable insights from the dataset summary."""
    llm    = get_llm()
    summary = state["raw_report"]
    performance_insights = _build_performance_insights(state["candidates_data"])

    if llm is None:
        state["error"] = f"LLM unavailable: {LLM_IMPORT_ERROR}" if LLM_IMPORT_ERROR else "LLM unavailable"
        state["insights"] = ["Learning fallback active: summary generated without LLM insights.", *performance_insights]
        state["pattern_updates"] = {}
        return state

    system = (
        "You are an AI recruiting analyst. "
        "Based on the data summary below, generate EXACTLY the following JSON structure:\n"
        "{\n"
        '  "insights": [<list of 5–8 concise insight strings>],\n'
        '  "pattern_updates": {\n'
        '    "technical_skills": <float 0.0–0.4>,\n'
        '    "answer_quality":   <float 0.0–0.4>,\n'
        '    "github_quality":   <float 0.0–0.3>,\n'
        '    "ai_penalty":       <float 0.0–0.3>,\n'
        '    "completeness":     <float 0.0–0.2>\n'
        "  },\n"
        '  "new_ai_phrases": [<list of new ChatGPT phrases discovered, if any>]\n'
        "}\n\n"
        "Ensure weights in pattern_updates sum to 1.0. Return ONLY valid JSON — no markdown."
    )

    try:
        resp = llm.invoke([
            SystemMessage(content=system),
            HumanMessage(content=summary),
        ])
        raw  = resp.content.strip().replace("```json","").replace("```","")
        data = json.loads(raw)
        state["insights"]        = data.get("insights", [])
        state["pattern_updates"] = data.get("pattern_updates", {})
        for insight in performance_insights:
            if insight not in state["insights"]:
                state["insights"].append(insight)
        # Append newly discovered phrases to the raw report for storage
        new_phrases = data.get("new_ai_phrases", [])
        if new_phrases:
            state["raw_report"] += f"\n\nNEW AI PHRASES DISCOVERED: {new_phrases}"
    except Exception as e:
        state["error"]    = str(e)
        state["insights"] = ["Analysis failed — see error log.", *performance_insights]
        state["pattern_updates"] = {}

    return state


# ── Graph

def build_learning_graph():
    if StateGraph is None:
        raise RuntimeError(f"LangGraph unavailable: {LANGGRAPH_IMPORT_ERROR}")
    g = StateGraph(LearningState)
    g.add_node("gather_node",   gather_patterns)
    g.add_node("insights_node", generate_insights)
    g.set_entry_point("gather_node")
    g.add_edge("gather_node",   "insights_node")
    g.add_edge("insights_node", END)
    return g.compile()


_graph = None

def _run_learning_fallback(initial: LearningState) -> LearningState:
    state = gather_patterns(initial)
    state = generate_insights(state)
    if LANGGRAPH_IMPORT_ERROR and not state["error"]:
        state["error"] = f"LangGraph fallback active: {LANGGRAPH_IMPORT_ERROR}"
    return state

def run_learning_cycle(candidates: List[dict], interactions: List[dict]) -> dict:
    """
    Run the self-learning cycle.
    candidates   — list of candidate dicts (with scores, answers, flags)
    interactions — list of interaction dicts (emails sent/received)
    Returns {"insights", "pattern_updates", "raw_report", "error"}
    """
    global _graph

    initial: LearningState = {
        "candidates_data":   candidates,
        "interactions_data": interactions,
        "insights":          [],
        "pattern_updates":   {},
        "raw_report":        "",
        "error":             "",
    }
    if StateGraph is None:
        result = _run_learning_fallback(initial)
    else:
        try:
            if _graph is None:
                _graph = build_learning_graph()
            result = _graph.invoke(initial)
        except Exception as e:
            if "LangGraph" in str(e) or "pydantic_core" in str(e):
                result = _run_learning_fallback(initial)
            else:
                raise
    return {
        "insights":        result["insights"],
        "pattern_updates": result["pattern_updates"],
        "raw_report":      result["raw_report"],
        "error":           result["error"],
    }


def should_run_analysis(candidate_count: int) -> bool:
    """Return True every ANALYSIS_INTERVAL candidates."""
    return candidate_count > 0 and candidate_count % ANALYSIS_INTERVAL == 0
