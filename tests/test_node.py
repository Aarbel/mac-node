"""Offline unit tests for Hermes mac-node (no Playwright, no browser)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import node  # noqa: E402
from ig_export import IgError, NAVIGATION_STEP_KEYS, SELECTOR_STEPS  # noqa: E402


class AuthMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token = "test-token-secret"

    def test_non_tailnet_ip_forbidden(self) -> None:
        ok, status, reason = node.check_auth("8.8.8.8", f"Bearer {self.token}", self.token)
        self.assertFalse(ok)
        self.assertEqual(status, 403)
        self.assertEqual(reason, "forbidden_ip")

    def test_tailnet_ip_bad_token(self) -> None:
        ok, status, reason = node.check_auth("100.64.0.1", "Bearer wrong", self.token)
        self.assertFalse(ok)
        self.assertEqual(status, 401)
        self.assertEqual(reason, "bad_token")

    def test_tailnet_ip_good_token(self) -> None:
        ok, status, reason = node.check_auth("100.100.1.2", f"Bearer {self.token}", self.token)
        self.assertTrue(ok)
        self.assertEqual(status, 200)
        self.assertEqual(reason, "ok")

    def test_missing_bearer(self) -> None:
        ok, status, _ = node.check_auth("100.64.1.1", None, self.token)
        self.assertFalse(ok)
        self.assertEqual(status, 401)


class StalenessTests(unittest.TestCase):
    def test_unknown_last_export_requests(self) -> None:
        self.assertTrue(node.should_request_export(None, False))

    def test_five_days_skips(self) -> None:
        now = node.datetime(2026, 10, 31, tzinfo=node.timezone.utc)
        self.assertFalse(node.should_request_export("2026-10-26", False, now=now))

    def test_twenty_days_requests(self) -> None:
        now = node.datetime(2026, 10, 31, tzinfo=node.timezone.utc)
        self.assertTrue(node.should_request_export("2026-10-11", False, now=now))

    def test_old_export_requests(self) -> None:
        self.assertTrue(node.should_request_export("2026-09-01", False, now=node.datetime.now(node.timezone.utc)))

    def test_pending_blocks_request(self) -> None:
        self.assertFalse(node.should_request_export(None, True))
        self.assertFalse(node.should_request_export("2026-01-01", True))


class StateRoundTripTests(unittest.TestCase):
    def test_state_json_round_trip_and_status_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            payload = {
                "last_export_at": "2026-10-01",
                "last_check_at": "2026-10-31T12:00:00+00:00",
                "pending_request": True,
                "last_error": None,
            }
            node.save_state(payload, path)
            loaded = node.load_state(path)
            self.assertEqual(loaded["last_export_at"], "2026-10-01")
            shaped = node.shape_status(loaded)
            self.assertIn("dumps", shaped)
            self.assertTrue(shaped["pending_request"])


class TaskDispatchTests(unittest.TestCase):
    def test_unknown_task_value_error(self) -> None:
        with self.assertRaises(ValueError):
            node.default_task_dispatcher("nope", {})

    def test_ig_error_becomes_ok_false(self) -> None:
        def dispatcher(name: str, params: dict) -> dict:
            raise IgError("selector failed", step="your_information")

        result = node.run_task_with_timeout(dispatcher, "ig_state", {}, timeout_s=5)
        self.assertFalse(result["ok"])
        self.assertIn("selector failed", result["error"])


class PlistRenderTests(unittest.TestCase):
    def test_install_sh_render_plist(self) -> None:
        install_sh = ROOT / "install.sh"
        self.assertTrue(install_sh.is_file())
        proc = subprocess.run(
            ["bash", str(install_sh), "--render-plist"],
            capture_output=True,
            text=True,
            check=True,
        )
        plist = proc.stdout
        self.assertIn("RunAtLoad", plist)
        self.assertIn("KeepAlive", plist)
        self.assertIn("node.py", plist)
        self.assertIn("--on-login", plist)
        self.assertIn(".hermes-mac/venv/bin/python3", plist)


class SelectorTableTests(unittest.TestCase):
    def test_navigation_steps_fr_en(self) -> None:
        self.assertGreater(len(NAVIGATION_STEP_KEYS), 0)
        for key in NAVIGATION_STEP_KEYS:
            step = SELECTOR_STEPS[key]
            self.assertIn("names_fr", step, msg=key)
            self.assertIn("names_en", step, msg=key)
            self.assertGreater(len(step["names_fr"]), 0, msg=key)
            self.assertGreater(len(step["names_en"]), 0, msg=key)


class RequirementsTests(unittest.TestCase):
    def test_playwright_pinned(self) -> None:
        req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertRegex(req, r"playwright==1\.\S+")


class HandlerAuthIntegrationTests(unittest.TestCase):
    def test_known_tasks_set(self) -> None:
        self.assertIn("ig_state", node.KNOWN_TASKS)
        self.assertIn("browse", node.KNOWN_TASKS)


if __name__ == "__main__":
    unittest.main()
