import unittest
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.tools.internshala_probe import (
    ProbeResult,
    _summarise_candidate_endpoints,
    classify_failure,
    detect_captcha_from_text,
    extract_login_error,
)


class InternshalaProbeTests(unittest.TestCase):
    def test_detect_captcha_from_text_finds_recaptcha_marker(self):
        self.assertTrue(detect_captcha_from_text("<div class='g-recaptcha'></div>"))

    def test_extract_login_error_picks_human_readable_error(self):
        message = extract_login_error("Header\nIncorrect password. Try again.\nFooter")
        self.assertEqual(message, "Incorrect password. Try again.")

    def test_classify_failure_returns_403_when_main_document_blocked(self):
        result = ProbeResult(
            started_at=0.0,
            login_url="https://internshala.com/login/user",
            load_outcome="loaded",
            main_document_status=403,
        )
        self.assertEqual(classify_failure(result), "http_403_on_login_page")

    def test_classify_failure_marks_captcha_block_when_submit_stays_on_page(self):
        result = ProbeResult(
            started_at=0.0,
            login_url="https://internshala.com/login/user",
            final_url="https://internshala.com/login/user",
            load_outcome="loaded",
            submit_attempted=True,
            submit_outcome="clicked",
            captcha_present_before_submit=True,
        )
        self.assertEqual(classify_failure(result), "captcha_blocked_submission_likely")

    def test_classify_failure_marks_submit_timeout(self):
        result = ProbeResult(
            started_at=0.0,
            login_url="https://internshala.com/login/user",
            load_outcome="loaded",
            submit_attempted=True,
            submit_outcome="submit_timeout",
        )
        self.assertEqual(classify_failure(result), "submit_timeout")

    def test_summarise_candidate_endpoints_filters_candidate_like_urls(self):
        summary = _summarise_candidate_endpoints(
            [
                {
                    "method": "GET",
                    "url": "https://internshala.com/employer/applications?page=2",
                    "status": 200,
                    "response_content_type": "application/json",
                    "response_body_preview": "{}",
                    "request_post_data": "",
                },
                {
                    "method": "POST",
                    "url": "https://internshala.com/info/storeData",
                    "status": 200,
                    "response_content_type": "application/json",
                    "response_body_preview": "{}",
                    "request_post_data": "",
                },
            ]
        )
        self.assertEqual(len(summary), 1)
        self.assertIn("/employer/applications", summary[0]["url"])


if __name__ == "__main__":
    unittest.main()
