#!/usr/bin/env python3
"""Hermes residential browser node — HTTP API and on-login export checks."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    import playwright  # noqa: F401

    _PLAYWRIGHT_INSTALLED = True
except ImportError:
    playwright = None  # type: ignore[assignment, misc]
    _PLAYWRIGHT_INSTALLED = False

import ig_export
from ig_export import IgError, has_session_cookie

HERMES_HOME = Path.home() / ".hermes-mac"
DEFAULT_TOKEN_FILE = HERMES_HOME / "token"
STATE_FILE = HERMES_HOME / "state.json"
LOG_FILE = HERMES_HOME / "node.log"
DUMPS_DIR = HERMES_HOME / "dumps"

TAILNET_NETWORK = ipaddress.ip_network("100.64.0.0/10")
TASK_TIMEOUT_S = 300
EXPORT_STALE_DAYS = 14

_REPO_DIR = Path(__file__).resolve().parent
_START_TIME = time.time()

_log_lock = threading.Lock()


def _setup_logging() -> logging.Logger:
    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("hermes.node")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


LOGGER = _setup_logging()


def load_token(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def is_tailscale_ip(ip_str: str) -> bool:
    try:
        return ipaddress.ip_address(ip_str) in TAILNET_NETWORK
    except ValueError:
        return False


def check_auth(
    client_ip: str,
    auth_header: Optional[str],
    token: str,
) -> Tuple[bool, int, str]:
    """Return (allowed, http_status, reason)."""
    if not is_tailscale_ip(client_ip):
        return False, 403, "forbidden_ip"
    if not auth_header or not auth_header.startswith("Bearer "):
        return False, 401, "missing_bearer"
    provided = auth_header[7:].strip()
    if not hmac.compare_digest(provided, token):
        return False, 401, "bad_token"
    return True, 200, "ok"


def load_state(path: Path = STATE_FILE) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: Dict[str, Any], path: Path = STATE_FILE) -> None:
    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def list_dumps(dumps_dir: Path = DUMPS_DIR) -> List[Dict[str, Any]]:
    if not dumps_dir.is_dir():
        return []
    items: List[Dict[str, Any]] = []
    for p in sorted(dumps_dir.iterdir()):
        if p.is_file():
            stat = p.stat()
            items.append(
                {
                    "name": p.name,
                    "size": stat.st_size,
                    "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                }
            )
    return items


def shape_status(state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    st = state if state is not None else load_state()
    return {
        "last_export_at": st.get("last_export_at"),
        "last_check_at": st.get("last_check_at"),
        "pending_request": bool(st.get("pending_request", False)),
        "dumps": list_dumps(),
        "last_error": st.get("last_error"),
    }


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        if "T" in value:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def should_request_export(
    last_export_at: Optional[str],
    pending: bool,
    *,
    now: Optional[datetime] = None,
    stale_days: int = EXPORT_STALE_DAYS,
) -> bool:
    if pending:
        return False
    ref = now or datetime.now(timezone.utc)
    last = _parse_date(last_export_at)
    if last is None:
        return True
    age = ref - last
    return age.days >= stale_days


def get_tailscale_ip() -> Optional[str]:
    for cmd in (
        ["tailscale", "ip", "-4"],
        ["/Applications/Tailscale.app/Contents/MacOS/Tailscale", "ip", "-4"],
    ):
        try:
            out = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if out.returncode == 0:
                line = out.stdout.strip().splitlines()[0].strip()
                if line:
                    return line
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def notify_macos(message: str, title: str = "Hermes") -> None:
    script = f'display notification "{message}" with title "{title}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def health_payload(token_file: Path) -> Dict[str, Any]:
    import platform

    logged_in = has_session_cookie()
    return {
        "ok": True,
        "ts": datetime.now(timezone.utc).isoformat(),
        "host": platform.node(),
        "macos": platform.system() == "Darwin",
        "python": platform.python_version(),
        "playwright": _PLAYWRIGHT_INSTALLED,
        "logged_in": logged_in,
        "tailscale_ip": get_tailscale_ip(),
        "uptime_s": int(time.time() - _START_TIME),
    }


TaskFn = Callable[[Dict[str, Any]], Dict[str, Any]]

KNOWN_TASKS = frozenset(
    {"ig_state", "ig_request_export", "ig_download", "browse", "ig_login"}
)


def default_task_dispatcher(name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if name == "ig_state":
        data = ig_export.state()
        return {"ok": True, "data": data}
    if name == "ig_request_export":
        data = ig_export.request_export()
        return {"ok": True, "data": data}
    if name == "ig_download":
        url = params.get("url")
        if not url:
            st = ig_export.state()
            url = st.get("download_url")
            if not url:
                raise IgError("No download URL in params or ig_state", step="ig_download")
        data = ig_export.download(str(url))
        return {"ok": True, "data": data}
    if name == "browse":
        url = params.get("url")
        if not url:
            raise IgError("params.url is required", step="browse")
        data = ig_export.browse(str(url))
        return {"ok": True, "data": data}
    if name == "ig_login":

        def _open_login() -> None:
            try:
                ig_export.login()
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("ig_login background error: %s", exc)

        threading.Thread(target=_open_login, daemon=True).start()
        return {"ok": True, "data": {"opened": True}}
    raise ValueError(f"unknown task: {name}")


def run_task_with_timeout(
    dispatcher: Callable[[str, Dict[str, Any]], Dict[str, Any]],
    name: str,
    params: Dict[str, Any],
    timeout_s: int = TASK_TIMEOUT_S,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"ok": False, "error": "timeout"}
    exc_holder: List[BaseException] = []

    def worker() -> None:
        nonlocal result
        try:
            result = dispatcher(name, params or {})
        except IgError as exc:
            result = {"ok": False, "error": str(exc), "step": exc.step}
        except ValueError as exc:
            result = {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            exc_holder.append(exc)
            result = {"ok": False, "error": str(exc)}

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=timeout_s)
    if thread.is_alive():
        return {"ok": False, "error": f"task timed out after {timeout_s}s"}
    if exc_holder:
        LOGGER.error("task %s failed: %s", name, exc_holder[0])
    return result


def on_login_check(
    *,
    state_path: Path = STATE_FILE,
    dispatcher: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
) -> str:
    dispatch = dispatcher or default_task_dispatcher
    state = load_state(state_path)
    summary_parts: List[str] = []
    try:
        ig_result = dispatch("ig_state", {})
        data = ig_result.get("data") or {}
        state["last_check_at"] = datetime.now(timezone.utc).isoformat()
        state["last_export_at"] = data.get("last_export_at")
        state["pending_request"] = bool(data.get("pending"))
        state["last_error"] = None

        if should_request_export(state.get("last_export_at"), state["pending_request"]):
            req = dispatch("ig_request_export", {})
            req_data = req.get("data") or {}
            if req_data.get("needs_password"):
                msg = "Export Meta : saisissez votre mot de passe dans la fenêtre navigateur."
                notify_macos(msg)
                summary_parts.append("needs_password")
            elif req.get("ok") and req_data.get("requested"):
                state["pending_request"] = True
                notify_macos("Demande d'export Instagram envoyée.")
                summary_parts.append("export_requested")
            elif not req.get("ok"):
                state["last_error"] = req.get("error")
                summary_parts.append("request_failed")
        else:
            summary_parts.append("export_ok")
    except Exception as exc:  # noqa: BLE001
        state["last_error"] = str(exc)
        summary_parts.append("error")
        LOGGER.error("on_login_check: %s", traceback.format_exc())

    save_state(state, state_path)
    summary = "on_login: " + ",".join(summary_parts)
    LOGGER.info(summary)
    print(summary)
    return summary


class HermesHandler(BaseHTTPRequestHandler):
    server_version = "HermesMacNode/1.0"
    token: str = ""
    task_dispatcher: Callable[[str, Dict[str, Any]], Dict[str, Any]] = default_task_dispatcher

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        pass

    def _client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def _log_request(self, path: str, status: int, started: float) -> None:
        duration_ms = int((time.time() - started) * 1000)
        with _log_lock:
            LOGGER.info(
                "request client=%s path=%s status=%s duration_ms=%s",
                self._client_ip(),
                path,
                status,
                duration_ms,
            )

    def _send_json(self, status: int, payload: Dict[str, Any], started: float) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self._log_request(self.path, status, started)

    def _unauthorized(self, started: float, code: int, error: str) -> None:
        self._send_json(code, {"ok": False, "error": error}, started)

    def _check_request_auth(self, started: float) -> bool:
        ok, status, reason = check_auth(
            self._client_ip(),
            self.headers.get("Authorization"),
            self.token,
        )
        if not ok:
            self._unauthorized(started, status, reason)
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        started = time.time()
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            if not self._check_request_auth(started):
                return
            self._send_json(200, health_payload(DEFAULT_TOKEN_FILE), started)
            return

        if path == "/status":
            if not self._check_request_auth(started):
                return
            self._send_json(200, shape_status(), started)
            return

        self._send_json(404, {"ok": False, "error": "not_found"}, started)

    def do_POST(self) -> None:  # noqa: N802
        started = time.time()
        parsed = urlparse(self.path)
        if parsed.path != "/task":
            self._send_json(404, {"ok": False, "error": "not_found"}, started)
            return
        if not self._check_request_auth(started):
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "invalid_json"}, started)
            return

        name = body.get("name")
        params = body.get("params") or {}
        if not name or not isinstance(name, str):
            self._send_json(400, {"ok": False, "error": "name required"}, started)
            return

        if name not in KNOWN_TASKS:
            self._send_json(400, {"ok": False, "error": f"unknown task: {name}"}, started)
            return

        result = run_task_with_timeout(self.task_dispatcher, name, params)
        if name == "ig_state" and result.get("ok"):
            st = load_state()
            data = result.get("data") or {}
            st["last_check_at"] = datetime.now(timezone.utc).isoformat()
            st["last_export_at"] = data.get("last_export_at")
            st["pending_request"] = bool(data.get("pending"))
            save_state(st)
        self._send_json(200, result, started)


def make_handler_class(
    token: str,
    dispatcher: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
) -> type:
    disp = dispatcher or default_task_dispatcher

    class ConfiguredHandler(HermesHandler):
        pass

    ConfiguredHandler.token = token
    ConfiguredHandler.task_dispatcher = disp
    return ConfiguredHandler


def run_server(
    bind: str,
    port: int,
    token_file: Path,
    dispatcher: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
) -> None:
    token = load_token(token_file)
    handler = make_handler_class(token, dispatcher)
    server = ThreadingHTTPServer((bind, port), handler)
    LOGGER.info("listening on %s:%s", bind, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes macOS browser node")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token-file", type=DEFAULT_TOKEN_FILE)
    parser.add_argument("--on-login", action="store_true", help="Run export check then serve")
    parser.add_argument("--check-only", action="store_true", help="Only run on-login check")
    args = parser.parse_args(argv)

    token_path = Path(args.token_file)

    if args.on_login or args.check_only:
        on_login_check()

    if args.check_only:
        return 0

    run_server(args.bind, args.port, token_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
