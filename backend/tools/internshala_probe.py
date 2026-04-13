"""
Playwright-based diagnostic probe for the Internshala login flow.

This script does not try to bypass CAPTCHA or anti-bot protections.
It only records what happened so we can describe the exact failure mode
from a real browser session.

Usage:
    python -m backend.tools.internshala_probe
    python -m backend.tools.internshala_probe --headless=false
    python -m backend.tools.internshala_probe --email user@example.com --password secret
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_DIR = PROJECT_ROOT / "tests" / ".artifacts" / "internshala_probe"
DEFAULT_LOGIN_URL = "https://internshala.com/login/user"

CAPTCHA_PATTERNS = (
    "recaptcha",
    "g-recaptcha",
    "grecaptcha",
    "hcaptcha",
    "cf-challenge",
    "turnstile",
)

LOGIN_ERROR_PATTERNS = (
    "incorrect",
    "invalid",
    "wrong password",
    "try again",
    "unable to log in",
    "unable to login",
    "login failed",
)


@dataclass
class ProbeResult:
    started_at: float
    login_url: str
    final_url: str = ""
    title: str = ""
    load_outcome: str = "not_started"
    failure_mode: str = "unknown"
    main_document_status: int | None = None
    submit_attempted: bool = False
    submit_outcome: str = "not_attempted"
    captcha_present_before_submit: bool = False
    captcha_present_after_submit: bool = False
    login_error_text: str = ""
    redirect_chain: list[str] = field(default_factory=list)
    console_messages: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    request_failures: list[str] = field(default_factory=list)
    response_log: list[dict[str, Any]] = field(default_factory=list)
    xhr_fetch_log: list[dict[str, Any]] = field(default_factory=list)
    candidate_endpoints: list[dict[str, Any]] = field(default_factory=list)
    login_detected: bool = False
    notes: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)


def _bool_arg(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _safe_slug(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in text.lower()).strip("_")
    return cleaned or "probe"


def _truncate_text(text: str, limit: int = 2000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def detect_captcha_from_text(text: str) -> bool:
    haystack = (text or "").lower()
    return any(pattern in haystack for pattern in CAPTCHA_PATTERNS)


def extract_login_error(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in lines:
        lowered = line.lower()
        if any(pattern in lowered for pattern in LOGIN_ERROR_PATTERNS):
            return line[:300]
    return ""


def classify_failure(result: ProbeResult) -> str:
    if result.load_outcome == "goto_timeout":
        return "page_load_timeout"
    if result.load_outcome == "goto_failed":
        return "page_load_failed"
    if result.main_document_status == 403:
        return "http_403_on_login_page"
    if result.submit_attempted and result.submit_outcome == "submit_timeout":
        return "submit_timeout"
    if result.submit_attempted and result.captcha_present_after_submit:
        return "captcha_present_after_submit"
    if result.submit_attempted and result.login_error_text:
        return "login_error_message_after_submit"
    if result.submit_attempted and result.final_url:
        same_url = result.final_url.rstrip("/") == result.login_url.rstrip("/")
        if same_url and result.captcha_present_before_submit:
            return "captcha_blocked_submission_likely"
        if same_url:
            return "stayed_on_login_page_after_submit"
    if len(result.redirect_chain) >= 4 and len(set(result.redirect_chain[-4:])) <= 2:
        return "possible_redirect_loop"
    if result.final_url and "login" not in result.final_url.lower():
        return "navigated_away_from_login_page"
    if result.captcha_present_before_submit:
        return "captcha_present_on_login_page"
    return "observed_without_clear_failure_mode"


def _summarise_candidate_endpoints(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        url = entry.get("url", "")
        if "internshala.com" not in url:
            continue
        haystack = " ".join(
            [
                url,
                entry.get("response_body_preview", ""),
                entry.get("request_post_data", ""),
            ]
        ).lower()
        if not any(token in haystack for token in ("candidate", "application", "applicant", "applications", "job_post", "job", "employer")):
            continue
        key = (entry.get("method", ""), url)
        if key not in seen:
            seen[key] = {
                "method": entry.get("method", ""),
                "url": url,
                "status": entry.get("status"),
                "response_content_type": entry.get("response_content_type", ""),
            }
    return list(seen.values())


async def _first_visible_locator(page, selectors: list[str]):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible(timeout=1000):
                return locator, selector
        except Exception:
            continue
    return None, ""


async def _detect_captcha(page) -> bool:
    html = await page.content()
    if detect_captcha_from_text(html):
        return True

    selectors = [
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        "iframe[src*='turnstile']",
        ".g-recaptcha",
        "[data-sitekey]",
    ]
    for selector in selectors:
        try:
            if await page.locator(selector).count():
                return True
        except Exception:
            continue
    return False


async def _collect_page_text(page) -> str:
    selectors = ["body", "main", "form"]
    fragments: list[str] = []
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count():
                fragments.append(await locator.inner_text(timeout=1000))
        except Exception:
            continue
    return "\n".join(fragment for fragment in fragments if fragment)


async def _switch_account_type(page, account_type: str, notes: list[str]) -> None:
    if account_type != "employer":
        return
    locator, selector = await _first_visible_locator(
        page,
        [
            "#employer",
            "text='Employer / T&P'",
            "text='Employer / T&P'",
        ],
    )
    if locator:
        try:
            await locator.click(timeout=5000)
            notes.append(f"account_type_selected={selector}")
            await page.wait_for_timeout(1500)
        except Exception as exc:
            notes.append(f"account_type_switch_failed: {exc}")
    else:
        notes.append("account_type_switch_not_found")


async def _write_artifacts(page, run_dir: Path, result: ProbeResult) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = run_dir / "page.png"
    html_path = run_dir / "page.html"
    json_path = run_dir / "result.json"
    xhr_json_path = run_dir / "xhr_fetch_details.json"

    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
        result.artifacts["screenshot"] = str(screenshot_path)
    except Exception as exc:
        result.notes.append(f"screenshot_failed: {exc}")

    try:
        html_path.write_text(await page.content(), encoding="utf-8")
        result.artifacts["html"] = str(html_path)
    except Exception as exc:
        result.notes.append(f"html_dump_failed: {exc}")

    xhr_json_path.write_text(json.dumps(result.xhr_fetch_log, indent=2), encoding="utf-8")
    result.artifacts["xhr_fetch_details"] = str(xhr_json_path)

    json_path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
    result.artifacts["json"] = str(json_path)


async def _safe_request_headers(request) -> dict[str, str]:
    try:
        headers_obj = request.all_headers
        headers = await headers_obj() if callable(headers_obj) else headers_obj
        return headers or {}
    except Exception:
        try:
            referer = await request.header_value("referer")
            return {"referer": referer} if referer else {}
        except Exception:
            return {}


async def _safe_response_headers(response) -> dict[str, str]:
    try:
        headers_obj = response.all_headers
        headers = await headers_obj() if callable(headers_obj) else headers_obj
        return headers or {}
    except Exception:
        return {}


def _safe_request_failure(request) -> str:
    try:
        failure = request.failure
        if isinstance(failure, dict):
            return failure.get("errorText", "unknown")
        if isinstance(failure, str):
            return failure
    except Exception:
        pass
    return "unknown"


def _safe_request_post_data(request) -> str:
    try:
        post_data = request.post_data
        if isinstance(post_data, str):
            return post_data
    except Exception:
        pass

    try:
        post_data_buffer = request.post_data_buffer
        if isinstance(post_data_buffer, (bytes, bytearray)):
            return post_data_buffer.decode("utf-8", errors="replace")
    except Exception:
        pass
    return ""


async def run_probe(args: argparse.Namespace) -> ProbeResult:
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise SystemExit(
            "Playwright is not installed. Run `pip install playwright` and "
            "`python -m playwright install chromium` before using this probe."
        ) from exc

    result = ProbeResult(started_at=time.time(), login_url=args.login_url)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = ARTIFACT_DIR / f"{timestamp}-{_safe_slug(args.label)}"

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=args.headless,
            slow_mo=args.slow_mo,
        )
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()
        pending_capture_tasks: set[asyncio.Task] = set()

        async def capture_xhr_fetch(response) -> None:
            if response.request.resource_type not in {"xhr", "fetch"}:
                return

            entry: dict[str, Any] = {
                "url": response.url,
                "method": response.request.method,
                "resource_type": response.request.resource_type,
                "status": response.status,
                "request_headers": {},
                "response_headers": {},
                "request_post_data": _safe_request_post_data(response.request),
                "response_body_preview": "",
                "response_content_type": "",
            }

            try:
                entry["request_headers"] = await _safe_request_headers(response.request)
            except Exception as exc:
                entry["request_headers_error"] = str(exc)

            try:
                entry["response_headers"] = await _safe_response_headers(response)
                entry["response_content_type"] = entry["response_headers"].get("content-type", "")
            except Exception as exc:
                entry["response_headers_error"] = str(exc)

            try:
                body_bytes = await response.body()
                content_type = entry.get("response_content_type", "").lower()
                if any(token in content_type for token in ("json", "text", "javascript", "html", "xml", "form-urlencoded")):
                    entry["response_body_preview"] = _truncate_text(body_bytes.decode("utf-8", errors="replace"))
                else:
                    entry["response_body_base64"] = base64.b64encode(body_bytes[:4096]).decode("ascii")
                    entry["response_body_note"] = "binary or unknown content-type; saved first 4096 bytes as base64"
            except Exception as exc:
                entry["response_body_error"] = str(exc)

            result.xhr_fetch_log.append(entry)

        def on_console(msg) -> None:
            text = f"{msg.type}: {msg.text}"
            if len(result.console_messages) < 100:
                result.console_messages.append(text)

        def on_page_error(error) -> None:
            if len(result.page_errors) < 50:
                result.page_errors.append(str(error))

        def on_request_failed(request) -> None:
            if len(result.request_failures) < 50:
                reason = _safe_request_failure(request)
                result.request_failures.append(f"{request.method} {request.url} -> {reason}")

        def on_response(response) -> None:
            if len(result.response_log) < 200:
                result.response_log.append(
                    {
                        "url": response.url,
                        "status": response.status,
                        "resource_type": response.request.resource_type,
                    }
                )
            task = asyncio.create_task(capture_xhr_fetch(response))
            pending_capture_tasks.add(task)
            task.add_done_callback(pending_capture_tasks.discard)

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("requestfailed", on_request_failed)
        page.on("response", on_response)
        page.on("framenavigated", lambda frame: result.redirect_chain.append(frame.url) if frame == page.main_frame else None)

        try:
            response = await page.goto(args.login_url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            result.load_outcome = "loaded"
            if response is not None:
                result.main_document_status = response.status
        except PlaywrightTimeoutError:
            result.load_outcome = "goto_timeout"
            result.failure_mode = classify_failure(result)
            result.final_url = page.url
            result.title = await page.title()
            await _write_artifacts(page, run_dir, result)
            await context.close()
            await browser.close()
            return result
        except Exception as exc:
            result.load_outcome = "goto_failed"
            result.notes.append(f"goto_error: {exc}")
            result.failure_mode = classify_failure(result)
            result.final_url = page.url
            try:
                result.title = await page.title()
            except Exception:
                pass
            await _write_artifacts(page, run_dir, result)
            await context.close()
            await browser.close()
            return result

        await page.wait_for_timeout(2500)
        result.captcha_present_before_submit = await _detect_captcha(page)
        await _switch_account_type(page, args.account_type, result.notes)

        email = args.email or os.getenv("INTERNSHALA_EMAIL", "")
        password = args.password or os.getenv("INTERNSHALA_PASSWORD", "")

        if email or password:
            email_loc, email_selector = await _first_visible_locator(
                page,
                [
                    "input[type='email']",
                    "input[name='email']",
                    "input[name='username']",
                    "#email",
                ],
            )
            password_loc, password_selector = await _first_visible_locator(
                page,
                [
                    "input[type='password']",
                    "input[name='password']",
                    "#password",
                ],
            )
            submit_loc, submit_selector = await _first_visible_locator(
                page,
                [
                    "button[type='submit']",
                    "input[type='submit']",
                    "button:has-text('Login')",
                    "button:has-text('Sign in')",
                ],
            )

            result.notes.append(f"email_selector={email_selector or 'not_found'}")
            result.notes.append(f"password_selector={password_selector or 'not_found'}")
            result.notes.append(f"submit_selector={submit_selector or 'not_found'}")

            if email_loc and email:
                await email_loc.fill(email)
            if password_loc and password:
                await password_loc.fill(password)

            if email and password and email_loc and password_loc and submit_loc:
                result.submit_attempted = True
                try:
                    await submit_loc.click(timeout=5000)
                    result.submit_outcome = "clicked"
                    await page.wait_for_load_state("networkidle", timeout=args.post_submit_timeout_ms)
                except PlaywrightTimeoutError:
                    result.submit_outcome = "submit_timeout"
                except Exception as exc:
                    result.submit_outcome = "submit_failed"
                    result.notes.append(f"submit_error: {exc}")
            else:
                if args.manual_login_wait_ms > 0:
                    result.notes.append(f"manual_login_wait_started={args.manual_login_wait_ms}")
                    await page.wait_for_timeout(args.manual_login_wait_ms)
                else:
                    result.notes.append("partial_credentials_present_but_submit_not_attempted")
        else:
            result.notes.append("no_credentials_provided_submit_skipped")
            if args.manual_login_wait_ms > 0:
                result.notes.append(f"manual_login_wait_started={args.manual_login_wait_ms}")
                await page.wait_for_timeout(args.manual_login_wait_ms)

        await page.wait_for_timeout(2500)
        if pending_capture_tasks:
            await asyncio.gather(*pending_capture_tasks, return_exceptions=True)
        page_text = await _collect_page_text(page)
        result.captcha_present_after_submit = await _detect_captcha(page)
        result.login_error_text = extract_login_error(page_text)
        result.final_url = page.url
        result.title = await page.title()
        result.login_detected = "login" not in result.final_url.lower()
        result.candidate_endpoints = _summarise_candidate_endpoints(result.xhr_fetch_log)
        result.failure_mode = classify_failure(result)

        await _write_artifacts(page, run_dir, result)
        await context.close()
        await browser.close()
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record Internshala login probe evidence.")
    parser.add_argument("--login-url", default=DEFAULT_LOGIN_URL, help="Login URL to inspect.")
    parser.add_argument(
        "--account-type",
        default="student",
        choices=["student", "employer"],
        help="Which login tab to activate before probing.",
    )
    parser.add_argument("--email", default="", help="Login email. Falls back to INTERNSHALA_EMAIL.")
    parser.add_argument("--password", default="", help="Login password. Falls back to INTERNSHALA_PASSWORD.")
    parser.add_argument("--headless", default="true", help="Run Chromium headless: true/false.")
    parser.add_argument("--slow-mo", default=0, type=int, help="Delay each Playwright action in ms.")
    parser.add_argument("--timeout-ms", default=30000, type=int, help="Initial page-load timeout in ms.")
    parser.add_argument(
        "--post-submit-timeout-ms",
        default=15000,
        type=int,
        help="Wait timeout after clicking submit.",
    )
    parser.add_argument(
        "--manual-login-wait-ms",
        default=0,
        type=int,
        help="When credentials are absent, keep the browser open for manual login before collecting artifacts.",
    )
    parser.add_argument("--label", default="manual", help="Artifact label for this run.")
    return parser


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    parser = build_parser()
    args = parser.parse_args()
    args.headless = _bool_arg(args.headless)
    result = asyncio.run(run_probe(args))
    print(json.dumps(asdict(result), indent=2))


if __name__ == "__main__":
    main()
