"""Offline unit tests for Hermes mac-node (no Playwright, no browser)."""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import argparse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ig_export  # noqa: E402
import node  # noqa: E402
from ig_export import (  # noqa: E402
    IgError,
    NAVIGATION_STEP_KEYS,
    SELECTOR_STEPS,
    has_session_cookie,
    iter_cookie_db_paths,
)


def _write_instagram_cookies_db(path: Path, *, include_session: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT)")
    if include_session:
        conn.execute(
            "INSERT INTO cookies (host_key, name) VALUES (?, ?)",
            (".instagram.com", "sessionid"),
        )
    conn.commit()
    conn.close()


HERMES_HANDLER_METHODS = frozenset(
    name
    for name, value in vars(node.BaseHTTPRequestHandler).items()
    if isinstance(value, types.FunctionType)
) | frozenset(
    name
    for name, value in vars(node.HermesHandler).items()
    if isinstance(value, types.FunctionType)
)


class ResolvePortTests(unittest.TestCase):
    def test_flag_over_env_and_file(self) -> None:
        port = node.resolve_port(9001, "8800", "8899")
        self.assertEqual(port, 9001)

    def test_env_over_file_and_default(self) -> None:
        port = node.resolve_port(None, "8800", "8899")
        self.assertEqual(port, 8800)

    def test_file_over_default(self) -> None:
        port = node.resolve_port(None, None, "8899\n")
        self.assertEqual(port, 8899)

    def test_default_when_nothing_set(self) -> None:
        self.assertEqual(node.resolve_port(None, None, None), node.DEFAULT_PORT)

    def test_garbage_file_falls_back_to_default(self) -> None:
        self.assertEqual(node.resolve_port(None, None, "not-a-port"), node.DEFAULT_PORT)

    def test_out_of_range_file_rejected(self) -> None:
        self.assertEqual(node.resolve_port(None, None, "80"), node.DEFAULT_PORT)
        self.assertEqual(node.resolve_port(None, None, "70000"), node.DEFAULT_PORT)

    def test_out_of_range_env_rejected(self) -> None:
        self.assertEqual(node.resolve_port(None, "22", "8899"), 8899)


class LoadStateTests(unittest.TestCase):
    def test_empty_file_returns_empty_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_bytes(b"")
            self.assertEqual(node.load_state(path), {})

    def test_corrupt_json_returns_empty_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_bytes(b"{not json")
            self.assertEqual(node.load_state(path), {})

    def test_valid_json_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps({"pending_request": True}), encoding="utf-8")
            loaded = node.load_state(path)
            self.assertTrue(loaded["pending_request"])


class BindConflictTests(unittest.TestCase):
    def test_bind_oserror_exits_code_3(self) -> None:
        class FailingServer:
            def __init__(self, address: tuple, handler: type) -> None:
                raise OSError("Address already in use")

        token_path = ROOT / "tests" / "fixtures" / "sample_state.json"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as tf:
            tf.write("test-token-for-bind\n")
            token_file = Path(tf.name)
        try:
            with mock.patch.object(node, "load_token", return_value="tok"):
                with self.assertRaises(SystemExit) as ctx:
                    node.run_server("127.0.0.1", 8791, token_file, server_class=FailingServer)
                self.assertEqual(ctx.exception.code, 3)
        finally:
            token_file.unlink(missing_ok=True)

    def test_bind_message_names_port(self) -> None:
        class FailingServer:
            def __init__(self, address: tuple, handler: type) -> None:
                raise OSError(node.errno.EADDRINUSE, "Address already in use")

        stderr = io.StringIO()
        with mock.patch.object(node, "load_token", return_value="tok"):
            with mock.patch.object(sys, "stderr", stderr):
                with self.assertRaises(SystemExit):
                    node.run_server("127.0.0.1", 8791, Path("/dev/null"), server_class=FailingServer)
        self.assertIn("8791", stderr.getvalue())
        self.assertIn("install.sh", stderr.getvalue())


class XForwardedForTests(unittest.TestCase):
    def _handler_with(
        self,
        peer: str,
        headers: dict | None = None,
    ) -> node.HermesHandler:
        handler = node.HermesHandler.__new__(node.HermesHandler)
        handler.client_address = (peer, 12345)
        handler.headers = headers or {}
        return handler

    def test_public_peer_spoofed_xff_uses_peer_for_auth(self) -> None:
        h = self._handler_with("8.8.8.8", {"X-Forwarded-For": "100.100.1.1"})
        self.assertEqual(h._client_ip(), "8.8.8.8")
        ok, status, _ = node.check_auth(h._client_ip(), "Bearer x", "x")
        self.assertFalse(ok)
        self.assertEqual(status, 403)

    def test_tailnet_peer_honours_xff(self) -> None:
        h = self._handler_with("100.64.0.5", {"X-Forwarded-For": "100.100.1.1"})
        self.assertEqual(h._client_ip(), "100.100.1.1")

    def test_no_header_uses_socket_peer(self) -> None:
        h = self._handler_with("100.100.1.2", {})
        self.assertEqual(h._client_ip(), "100.100.1.2")


class HandlerClassAttrTests(unittest.TestCase):
    def test_task_dispatcher_is_staticmethod_on_base_and_configured(self) -> None:
        self.assertIsInstance(node.HermesHandler.__dict__["task_dispatcher"], staticmethod)
        configured = node.make_handler_class("tok", lambda _n, _p: {"ok": True})
        self.assertIsInstance(configured.__dict__["task_dispatcher"], staticmethod)

    def test_no_unexpected_bare_functions_on_handler(self) -> None:
        for name, value in node.HermesHandler.__dict__.items():
            if not isinstance(value, types.FunctionType):
                continue
            self.assertIn(
                name,
                HERMES_HANDLER_METHODS,
                msg=f"{name} must be a method or staticmethod, not a bare function attribute",
            )

    def test_callable_class_attributes_use_staticmethod(self) -> None:
        for cls in (node.HermesHandler, node.make_handler_class("t", lambda _n, _p: {"ok": True})):
            for name, value in cls.__dict__.items():
                if name.startswith("__") or name in HERMES_HANDLER_METHODS:
                    continue
                if isinstance(value, staticmethod):
                    continue
                if isinstance(value, types.FunctionType):
                    self.fail(f"{cls.__name__}.{name} must be staticmethod, not a bare function")
                if callable(value):
                    self.fail(f"{cls.__name__}.{name} unexpected callable class attribute")


class TaskHttpIntegrationTests(unittest.TestCase):
    def test_post_task_over_http_uses_stub_dispatcher(self) -> None:
        token = "integration-test-token"

        def stub_dispatcher(name: str, params: dict) -> dict:
            self.assertEqual(name, "ig_state")
            self.assertEqual(params, {})
            return {"ok": True, "data": {"stub": True}}

        handler_cls = node.make_handler_class(token, stub_dispatcher)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        host, port = server.server_address[0], server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = json.dumps({"name": "ig_state", "params": {}}).encode("utf-8")
            request = urllib.request.Request(
                f"http://{host}:{port}/task",
                data=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with mock.patch.object(node, "is_tailscale_ip", return_value=True):
                with urllib.request.urlopen(request, timeout=10) as response:
                    self.assertEqual(response.status, 200)
                    body = json.loads(response.read().decode("utf-8"))
            self.assertTrue(body.get("ok"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class SessionCookieTests(unittest.TestCase):
    def test_default_cookies_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            db = base / "Default" / "Cookies"
            _write_instagram_cookies_db(db, include_session=True)
            self.assertTrue(has_session_cookie(base))

    def test_default_network_cookies_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            db = base / "Default" / "Network" / "Cookies"
            _write_instagram_cookies_db(db, include_session=True)
            self.assertTrue(has_session_cookie(base))

    def test_profile_subdir_cookies_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            db = base / "Profile 1" / "Default" / "Cookies"
            _write_instagram_cookies_db(db, include_session=True)
            self.assertTrue(has_session_cookie(base))

    def test_readable_db_without_session_is_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            db = base / "Default" / "Cookies"
            _write_instagram_cookies_db(db, include_session=False)
            self.assertFalse(has_session_cookie(base))

    def test_empty_db_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            db = base / "Default" / "Cookies"
            db.parent.mkdir(parents=True)
            db.write_bytes(b"")
            self.assertIsNone(has_session_cookie(base))

    def test_corrupt_db_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            db = base / "Default" / "Cookies"
            db.parent.mkdir(parents=True)
            db.write_bytes(b"not an sqlite database")
            self.assertIsNone(has_session_cookie(base))

    def test_iter_cookie_db_paths_includes_expected_locations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            paths = {
                base / "Default" / "Cookies",
                base / "Default" / "Network" / "Cookies",
                base / "Profile 2" / "Default" / "Cookies",
            }
            for p in paths:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"")
            found = set(iter_cookie_db_paths(base))
            self.assertTrue(paths.issubset(found))


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
        self.assertIn("<key>ThrottleInterval</key>", plist)
        self.assertIn("<integer>30</integer>", plist)
        self.assertIn("--port", plist)
        self.assertRegex(plist, r"<string>--port</string>\s*\n\s*<string>\d+</string>")


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


class ArgparseAuditTests(unittest.TestCase):
    def _assert_parser_sane(self, parser: argparse.ArgumentParser) -> None:
        for action in parser._actions:
            if action.type is not None:
                self.assertTrue(callable(action.type), msg=f"{action.dest} type must be callable")
            if action.default is argparse.SUPPRESS:
                continue
            self.assertFalse(
                callable(action.default),
                msg=f"{action.dest} default must not be a callable (use type= for converters)",
            )

    def test_node_parser_actions_sane(self) -> None:
        self._assert_parser_sane(node.build_parser())

    def test_ig_export_parser_actions_sane(self) -> None:
        import ig_export

        self._assert_parser_sane(ig_export.build_parser())

    def test_node_main_help_exits_zero(self) -> None:
        self.assertEqual(node.main(["--help"]), 0)

    def test_node_parser_default_token_file(self) -> None:
        args = node.build_parser().parse_args([])
        self.assertEqual(args.token_file, node.DEFAULT_TOKEN_FILE)

    def test_node_main_invalid_argument_exits_four(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            code = node.main(["--not-a-real-flag"])
        self.assertEqual(code, 4)
        self.assertIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
