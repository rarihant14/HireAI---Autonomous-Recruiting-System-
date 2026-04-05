"""
backend/agents/engagement_agent.py — Component 3: Engagement
Multi-round email conversations powered by LangGraph + Groq.
Uses Gmail API (credentials.json + token.json) as the primary transport,
with SMTP kept only as a send fallback if Gmail API is unavailable.

Flow per incoming reply:
  classify_reply → generate_response → send_email → update_state
"""

import re
from typing import TypedDict, List, Optional

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

from backend.config import cfg
from backend.tools.code_evaluator import evaluate_code_submission
from backend.utils  import log, safe_json_loads
from backend.tools.gmail_client import (
    send_email,
    fetch_unread_messages,
    send_initial_outreach,
)

def get_llm():
    if ChatGroq is None:
        return None
    return ChatGroq(
        api_key     = cfg.GROQ_API_KEY,
        model       = cfg.GROQ_MODEL,
        temperature = 0.5,
        max_tokens  = 1024,
    )

class EngagementState(TypedDict):
    candidate_id:         int
    candidate_name:       str
    candidate_email:      str
    job_role:             str
    round_number:         int
    thread_id:            Optional[str]
    conversation_history: List[dict]
    latest_reply:         str
    reply_type:           str
    next_email_body:      str
    subject:              str
    should_advance:       bool
    ai_score:             float
    ai_review:            str
    code_feedback:        str
    error:                str


def _infer_latest_question(state: EngagementState) -> str:
    """Best-effort recovery of the last recruiter prompt for AI checking."""
    for message in reversed(state["conversation_history"]):
        role = (message.get("role") or "").lower()
        if role in {"recruiter", "sent", "assistant"}:
            content = (message.get("content") or "").strip()
            if content:
                return content
    return state.get("subject", "") or "Please answer the technical prompt in the current email thread."


def _build_reply_review(state: EngagementState, llm) -> tuple[str, str]:
    """
    Run anti-cheat on the latest reply and ask Groq for a concise internal review.
    Returns (ai_review, code_feedback).
    """
    ai_review = ""
    code_feedback = ""
    question = _infer_latest_question(state)

    try:
        from backend.agents.anti_cheat_agent import check_candidate_response

        ai_check = check_candidate_response(
            candidate_id=state["candidate_id"],
            question=question,
            answer=state["latest_reply"],
            reply_latency=-1,
        )
        ai_review = (
            f"AI check: score={ai_check['ai_score']:.2f}, "
            f"flagged={ai_check['ai_flagged']}, flags={', '.join(ai_check['flags']) or 'none'}. "
            f"{ai_check['ai_explanation']}"
        )
        state["ai_score"] = float(ai_check["ai_score"])
        state["ai_review"] = ai_review
    except Exception as e:
        ai_review = f"AI check unavailable: {e}"
        state["ai_score"] = 0.0
        state["ai_review"] = ai_review

    if state["reply_type"] == "code":
        static_review = evaluate_code_submission(state["latest_reply"])
        static_summary = static_review["summary"]
        detail_text = " ".join(static_review.get("details", [])[:4])

        review_prompt = (
            f"Candidate reply:\n{state['latest_reply']}\n\n"
            f"Static code analysis:\n{static_summary}\n{detail_text}\n\n"
            f"AI check summary:\n{ai_review}\n\n"
            "Write a concise internal recruiter review in 3 bullet-style sentences. "
            "Mention correctness, likely quality of reasoning, and one follow-up angle."
        )
        try:
            resp = llm.invoke([
                SystemMessage(content="You are a senior engineer reviewing a candidate's emailed code submission."),
                HumanMessage(content=review_prompt),
            ])
            code_feedback = resp.content.strip()
        except Exception as e:
            code_feedback = f"{static_summary} {detail_text}".strip()
            if not state["error"]:
                state["error"] = str(e)

        state["code_feedback"] = code_feedback

    return ai_review, code_feedback

def classify_reply(state: EngagementState) -> EngagementState:
    llm   = get_llm()
    if llm is None:
        state["reply_type"] = "other"
        state["error"] = f"LLM unavailable: {LLM_IMPORT_ERROR}" if LLM_IMPORT_ERROR else "LLM unavailable"
        return state
    system = (
        "Classify the following job applicant email reply into exactly one category: "
        "technical, vague, code, question, other. "
        'Return ONLY JSON: {"type": "<category>", "key_point": "<one sentence summary>"}.'
    )
    try:
        resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=state["latest_reply"])])
        data = safe_json_loads(resp.content, fallback={})
        state["reply_type"] = data.get("type", "other")
    except Exception as e:
        state["reply_type"] = "other"
        log("Engagement", f"Classify error: {e}", "WARN")
    return state

def generate_response(state: EngagementState) -> EngagementState:
    llm  = get_llm()
    name = state["candidate_name"]
    ai_review = ""
    code_feedback = ""
    if llm is None:
        state["next_email_body"] = f"Hi {name},\n\nThank you for your reply. Could you elaborate a bit more?\n\nThe Hiring Team"
        if not state["error"]:
            state["error"] = f"LLM unavailable: {LLM_IMPORT_ERROR}" if LLM_IMPORT_ERROR else "LLM unavailable"
        if not state["subject"].startswith("Re:"):
            state["subject"] = "Re: " + state["subject"]
        state["should_advance"] = True
        return state
    type_instructions = {
        "technical": "The candidate described a specific technical approach. Acknowledge it by name. Ask them to go deeper: edge cases, scalability, or a real implementation challenge.",
        "vague":     "The candidate gave a vague answer. Ask a very specific follow-up — a command, library, or code snippet that proves real knowledge.",
        "code":      "The candidate shared code. Acknowledge it positively. Ask one sharp question about an edge case or error handling.",
        "question":  "The candidate asked a question. Give a direct helpful answer. Then pivot with a relevant technical follow-up.",
        "other":     "Respond helpfully and keep the conversation moving. Ask a specific technical question relevant to the role.",
    }
    conv_context = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in state["conversation_history"][-6:])
    ai_review, code_feedback = _build_reply_review(state, llm)

    system = (
        f"You are a senior technical recruiter hiring for: {state['job_role']}. "
        "Write a SHORT (3–6 sentences), professional, specific email reply. "
        "Reference something specific the candidate wrote — never be generic. "
        f"{type_instructions.get(state['reply_type'], type_instructions['other'])} "
        f"{'Use this AI-check context: ' + ai_review if ai_review else ''} "
        f"{'Use this code review context: ' + code_feedback if code_feedback else ''} "
        f"Start with 'Hi {name},' and sign off as 'The Hiring Team'. "
        "Return ONLY the email body — no subject line."
    )
    try:
        resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=f"Conversation:\n{conv_context}\n\nLatest reply:\n{state['latest_reply']}")])
        state["next_email_body"] = resp.content.strip()
    except Exception as e:
        state["next_email_body"] = f"Hi {name},\n\nThank you for your reply. Could you elaborate a bit more?\n\nThe Hiring Team"
        state["error"] = str(e)
    if not state["subject"].startswith("Re:"):
        state["subject"] = "Re: " + state["subject"]
    state["should_advance"] = True
    return state

def send_reply(state: EngagementState) -> EngagementState:
    result = send_email(
        to_addr    = state["candidate_email"],
        subject    = state["subject"],
        body_plain = state["next_email_body"],
        thread_id  = state["thread_id"],
    )
    if not result.get("success"):
        state["error"] = result.get("error", "Send failed")
    else:
        if result.get("thread_id"):
            state["thread_id"] = result["thread_id"]
        state["conversation_history"].append({"role": "recruiter", "content": state["next_email_body"]})
        if state["should_advance"]:
            state["round_number"] += 1
        log("Engagement", f"Sent round {state['round_number']} to {state['candidate_email']}")
    return state

def build_engagement_graph():
    if StateGraph is None:
        raise RuntimeError(f"LangGraph unavailable: {LANGGRAPH_IMPORT_ERROR}")
    g = StateGraph(EngagementState)
    g.add_node("classify", classify_reply)
    g.add_node("generate", generate_response)
    g.add_node("send",     send_reply)
    g.set_entry_point("classify")
    g.add_edge("classify", "generate")
    g.add_edge("generate", "send")
    g.add_edge("send",     END)
    return g.compile()

_graph = None

def _run_engagement_fallback(initial: EngagementState) -> EngagementState:
    state = classify_reply(initial)
    state = generate_response(state)
    state = send_reply(state)
    if LANGGRAPH_IMPORT_ERROR and not state["error"]:
        state["error"] = f"LangGraph fallback active: {LANGGRAPH_IMPORT_ERROR}"
    return state

def process_candidate_reply(candidate_id, candidate_name, candidate_email,
                            job_role, round_number, thread_id, subject,
                            latest_reply, conversation_history) -> dict:
    global _graph
    initial: EngagementState = {
        "candidate_id": candidate_id, "candidate_name": candidate_name,
        "candidate_email": candidate_email, "job_role": job_role,
        "round_number": round_number, "thread_id": thread_id or None,
        "conversation_history": conversation_history, "latest_reply": latest_reply,
        "reply_type": "other", "next_email_body": "", "subject": subject,
        "should_advance": False, "ai_score": 0.0, "ai_review": "", "code_feedback": "", "error": "",
    }
    if StateGraph is None:
        return _run_engagement_fallback(initial)
    try:
        if _graph is None:
            _graph = build_engagement_graph()
        return _graph.invoke(initial)
    except Exception as e:
        if "LangGraph" in str(e) or "pydantic_core" in str(e):
            return _run_engagement_fallback(initial)
        raise

def send_initial_email(candidate_name: str, candidate_email: str, job_role: str) -> dict:
    """Generate a Round 1 question with Groq, send via Gmail first."""
    llm = get_llm()
    try:
        if llm is None:
            raise RuntimeError(LLM_IMPORT_ERROR or "LLM unavailable")
        resp = llm.invoke([
            SystemMessage(content=f"You are a recruiter hiring for: {job_role}. Write ONE specific open-ended technical screening question. Return ONLY the question."),
            HumanMessage(content="Write the question."),
        ])
        question = resp.content.strip()
    except Exception:
        question = "Could you walk us through a recent technical project and your specific contribution?"
    return send_initial_outreach(to_addr=candidate_email, name=candidate_name, job_role=job_role, question=question)

def fetch_new_replies() -> List[dict]:
    """Poll unread replies via Gmail API."""
    messages = fetch_unread_messages(max_results=50)
    return [{"from": m["from_email"], "subject": m["subject"], "body": m["body"],
             "thread_id": m["thread_id"], "received_at": m["date"]} for m in messages]
