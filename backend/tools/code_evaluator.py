"""
backend/tools/code_evaluator.py - Lightweight candidate code review helpers.

This keeps evaluation safe by avoiding arbitrary execution. For Python snippets
we still perform a syntax compile check so the recruiter agent can respond with
specific feedback when a candidate shares runnable-looking code.
"""

from __future__ import annotations

import re
from typing import Any


def _extract_code_blocks(text: str) -> list[dict[str, str]]:
    blocks = []
    for match in re.finditer(r"```(?P<lang>[A-Za-z0-9_+-]*)\n(?P<code>.*?)```", text or "", re.DOTALL):
        blocks.append({
            "language": (match.group("lang") or "").strip().lower(),
            "code": (match.group("code") or "").strip(),
        })

    if blocks:
        return blocks

    # Fallback for inline code-like replies with no fenced block.
    if re.search(r"\b(def |class |import |from |function |const |let |var )", text or ""):
        return [{"language": "", "code": (text or "").strip()}]
    return []


def _guess_language(language: str, code: str) -> str:
    if language:
        return language
    if re.search(r"\bdef\s+\w+\(|\bimport\s+\w+|\bfrom\s+\w+\s+import\b|print\(", code):
        return "python"
    if re.search(r"\bfunction\s+\w+\(|=>|console\.log\(", code):
        return "javascript"
    return "unknown"


def evaluate_code_submission(text: str) -> dict[str, Any]:
    """
    Review shared code safely and return structured recruiter-facing feedback.
    """
    blocks = _extract_code_blocks(text)
    if not blocks:
        return {
            "contains_code": False,
            "summary": "The reply was classified as code-related, but no executable snippet was found.",
            "details": [],
        }

    findings: list[str] = []
    contains_python = False
    syntax_ok = True

    for idx, block in enumerate(blocks, start=1):
        code = block["code"]
        language = _guess_language(block["language"], code)
        line_count = len([line for line in code.splitlines() if line.strip()])
        findings.append(f"Snippet {idx}: detected {language}, {line_count} non-empty lines.")

        if language == "python":
            contains_python = True
            try:
                compile(code, f"<candidate_snippet_{idx}>", "exec")
                findings.append(f"Snippet {idx}: Python syntax check passed.")
            except SyntaxError as e:
                syntax_ok = False
                findings.append(
                    f"Snippet {idx}: Python syntax error on line {e.lineno}: {e.msg}."
                )

        if "try:" not in code and "except" not in code and "catch" not in code:
            findings.append(f"Snippet {idx}: no explicit error handling shown.")
        if "test" not in code.lower() and "assert" not in code:
            findings.append(f"Snippet {idx}: no test or assertion signal detected.")

    if contains_python and syntax_ok:
        summary = "The shared Python snippet passes a syntax compile check and looks structurally reviewable."
    elif contains_python and not syntax_ok:
        summary = "The shared Python snippet does not compile cleanly, so the recruiter should ask for a corrected version."
    else:
        summary = "The reply includes code, but only lightweight static review was possible for the detected language."

    return {
        "contains_code": True,
        "summary": summary,
        "details": findings,
    }

