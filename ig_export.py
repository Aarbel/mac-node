"""Instagram / Meta data export automation via Playwright (persistent profile)."""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright  # noqa: F811 — module-level guard for health checks
except ImportError:
    PlaywrightError = Exception  # type: ignore[misc, assignment]
    PlaywrightTimeoutError = Exception  # type: ignore[misc, assignment]
    sync_playwright = None  # type: ignore[assignment]
    playwright = None

HERMES_HOME = Path.home() / ".hermes-mac"
PROFILE_DIR = HERMES_HOME / "profile"
DUMPS_DIR = HERMES_HOME / "dumps"

VIEWPORT = {"width": 1280, "height": 900}
LOCALE = "fr-FR"
TIMEZONE = "Europe/Paris"
CONTEXT_ARGS = ["--no-first-run", "--no-default-browser-check"]
TEXT_TRUNCATE = 20_000

# Navigation steps: each step has French and English accessible-name variants.
# Tests assert every step includes both languages.
# Steps validated by tests (each must define names_fr and names_en).
NAVIGATION_STEP_KEYS = (
    "your_information",
    "download_information",
    "create_export",
    "instagram_profile",
    "format_json",
    "period_all_time",
    "download_to_device",
    "messages_only",
    "confirm_create",
)

SELECTOR_STEPS: Dict[str, Dict[str, List[str]]] = {
    "accounts_center_url": {
        "urls": [
            "https://accountscenter.instagram.com/",
            "https://www.instagram.com/accounts/center/",
        ],
    },
    "your_information": {
        "names_fr": [
            "Vos informations et autorisations",
            "Vos informations",
        ],
        "names_en": [
            "Your information and permissions",
            "Your information",
        ],
    },
    "download_information": {
        "names_fr": [
            "Télécharger vos informations",
            "Télécharger vos données",
        ],
        "names_en": [
            "Download your information",
            "Download your data",
        ],
    },
    "create_export": {
        "names_fr": [
            "Créer des fichiers",
            "Créer une exportation",
            "Demander une copie",
        ],
        "names_en": [
            "Create files",
            "Create export",
            "Request a copy",
        ],
    },
    "instagram_profile": {
        "names_fr": [
            "Instagram",
            "Profil Instagram",
        ],
        "names_en": [
            "Instagram",
            "Instagram profile",
        ],
    },
    "format_json": {
        "names_fr": [
            "JSON",
        ],
        "names_en": [
            "JSON",
        ],
    },
    "period_all_time": {
        "names_fr": [
            "Toutes les périodes",
            "Depuis le début",
            "All time",
        ],
        "names_en": [
            "All time",
            "All of my data",
        ],
    },
    "download_to_device": {
        "names_fr": [
            "Télécharger sur l'appareil",
            "Télécharger sur l’appareil",
            "Sur l'appareil",
        ],
        "names_en": [
            "Download to device",
            "On your device",
        ],
    },
    "messages_only": {
        "names_fr": [
            "Messages",
            "Messagerie",
        ],
        "names_en": [
            "Messages",
            "Messaging",
        ],
    },
    "confirm_create": {
        "names_fr": [
            "Créer des fichiers",
            "Créer",
            "Confirmer",
        ],
        "names_en": [
            "Create files",
            "Create",
            "Confirm",
        ],
    },
}


class SelectorError(Exception):
    """Raised when no selector matched at a navigation step."""

    def __init__(self, message: str, step: str = "", selectors_tried: Optional[List[str]] = None):
        self.step = step
        self.selectors_tried = selectors_tried or []
        super().__init__(message)


class IgError(Exception):
    """Automation failure surfaced to node.py."""

    def __init__(self, message: str, step: str = "", selectors_tried: Optional[List[str]] = None):
        self.step = step
        self.selectors_tried = selectors_tried or []
        super().__init__(message)


def _require_playwright() -> None:
    if sync_playwright is None:
        raise IgError("Playwright is not installed", step="import")


def _all_name_variants(step_key: str) -> List[str]:
    step = SELECTOR_STEPS.get(step_key, {})
    names: List[str] = []
    for key in ("names_fr", "names_en", "urls"):
        names.extend(step.get(key, []))
    return names


def _click_by_accessible_names(
    page: Any,
    step_key: str,
    timeout_ms: int = 30_000,
) -> None:
    variants = _all_name_variants(step_key)
    if not variants:
        raise SelectorError(
            f"No selector variants defined for step '{step_key}'",
            step=step_key,
            selectors_tried=[],
        )
    deadline = time.time() + timeout_ms / 1000.0
    last_error: Optional[str] = None
    while time.time() < deadline:
        for name in variants:
            try:
                locator = page.get_by_role("link", name=re.compile(re.escape(name), re.I))
                if locator.count() > 0:
                    locator.first.click(timeout=5000)
                    return
            except Exception as exc:  # noqa: BLE001 — collect and retry
                last_error = str(exc)
            try:
                locator = page.get_by_role("button", name=re.compile(re.escape(name), re.I))
                if locator.count() > 0:
                    locator.first.click(timeout=5000)
                    return
            except Exception as exc:
                last_error = str(exc)
            try:
                locator = page.get_by_text(re.compile(re.escape(name), re.I))
                if locator.count() > 0:
                    locator.first.click(timeout=5000)
                    return
            except Exception as exc:
                last_error = str(exc)
        page.wait_for_timeout(500)
    raise SelectorError(
        f"Step '{step_key}' failed: no matching control for variants {variants!r}"
        + (f" (last: {last_error})" if last_error else ""),
        step=step_key,
        selectors_tried=variants,
    )


def _launch_context(headless: bool = True) -> Tuple[Any, Any, Any]:
    _require_playwright()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        args=CONTEXT_ARGS,
        viewport=VIEWPORT,
        locale=LOCALE,
        timezone_id=TIMEZONE,
    )
    page = context.pages[0] if context.pages else context.new_page()
    return pw, context, page


def _close(pw: Any, context: Any) -> None:
    try:
        context.close()
    finally:
        pw.stop()


def has_session_cookie(profile_dir: Optional[Path] = None) -> bool:
    """Read Chromium Cookies DB (read-only) for Instagram sessionid."""
    base = profile_dir or PROFILE_DIR
    cookies_db = base / "Default" / "Cookies"
    if not cookies_db.is_file():
        cookies_db = base / "Cookies"
    if not cookies_db.is_file():
        return False
    import sqlite3

    uri = f"file:{cookies_db}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM cookies WHERE host_key LIKE '%instagram.com%' AND name = 'sessionid' LIMIT 1"
        )
        row = cur.fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def login() -> Dict[str, Any]:
    """Open headed browser; wait up to 300s for sessionid cookie."""
    _require_playwright()
    pw, context, page = _launch_context(headless=False)
    try:
        page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded")
        deadline = time.time() + 300
        while time.time() < deadline:
            cookies = context.cookies("https://www.instagram.com")
            if any(c.get("name") == "sessionid" for c in cookies):
                return {"logged_in": True}
            page.wait_for_timeout(1000)
        raise IgError("Login timed out waiting for sessionid cookie", step="login")
    finally:
        _close(pw, context)


def _parse_export_rows(page: Any) -> List[Dict[str, str]]:
    """Best-effort parse of export list from page text."""
    body = page.inner_text("body")
    exports: List[Dict[str, str]] = []
    date_re = re.compile(r"(\d{4}-\d{2}-\d{2})")
    status_words = ("In progress", "Ready", "Expired", "En cours", "Disponible", "Expiré")
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        m = date_re.search(line)
        if not m:
            continue
        status = "Unknown"
        for word in status_words:
            if word.lower() in line.lower():
                status = word
                break
        exports.append(
            {
                "created": m.group(1),
                "status": status,
                "label": line[:200],
            }
        )
    return exports


def _newest_ready_download_url(page: Any) -> Optional[str]:
    links = page.locator("a[href*='download'], a[href*='export']")
    count = links.count()
    for i in range(count):
        href = links.nth(i).get_attribute("href")
        if href and ("download" in href.lower() or "export" in href.lower()):
            return href
    return None


def state() -> Dict[str, Any]:
    _require_playwright()
    pw, context, page = _launch_context(headless=True)
    try:
        urls = SELECTOR_STEPS["accounts_center_url"]["urls"]
        loaded = False
        for url in urls:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                loaded = True
                break
            except PlaywrightTimeoutError:
                continue
        if not loaded:
            raise IgError("Could not load Accounts Center", step="accounts_center_url")

        _click_by_accessible_names(page, "your_information")
        page.wait_for_timeout(1500)
        _click_by_accessible_names(page, "download_information")
        page.wait_for_timeout(2000)

        exports = _parse_export_rows(page)
        pending = any(
            s in e.get("status", "").lower()
            for e in exports
            for s in ("progress", "cours", "pending")
        )
        last_export_at: Optional[str] = None
        if exports:
            dates = sorted(e["created"] for e in exports if e.get("created"))
            if dates:
                last_export_at = dates[-1]

        download_url = _newest_ready_download_url(page)
        result: Dict[str, Any] = {
            "exports": exports,
            "pending": pending,
            "last_export_at": last_export_at,
        }
        if download_url:
            result["download_url"] = download_url
        return result
    except SelectorError as exc:
        raise IgError(str(exc), step=exc.step, selectors_tried=exc.selectors_tried) from exc
    finally:
        _close(pw, context)


def _untick_all_except_messages(page: Any) -> None:
    """Uncheck all data categories except Messages."""
    checkboxes = page.locator('input[type="checkbox"]')
    count = checkboxes.count()
    for i in range(count):
        cb = checkboxes.nth(i)
        try:
            label_text = cb.evaluate(
                "el => (el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('aria-label') || ''"
            )
        except Exception:
            label_text = ""
        is_messages = any(
            re.search(re.escape(v), label_text or "", re.I)
            for v in _all_name_variants("messages_only")
        )
        try:
            checked = cb.is_checked()
        except Exception:
            continue
        if is_messages:
            if not checked:
                cb.check()
        else:
            if checked:
                cb.uncheck()


def request_export() -> Dict[str, Any]:
    _require_playwright()
    pw, context, page = _launch_context(headless=False)
    try:
        urls = SELECTOR_STEPS["accounts_center_url"]["urls"]
        for url in urls:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                break
            except PlaywrightTimeoutError:
                continue

        _click_by_accessible_names(page, "your_information")
        page.wait_for_timeout(1000)
        _click_by_accessible_names(page, "download_information")
        page.wait_for_timeout(1000)

        try:
            _click_by_accessible_names(page, "create_export")
        except SelectorError:
            pass

        try:
            _click_by_accessible_names(page, "instagram_profile")
        except SelectorError:
            pass

        _untick_all_except_messages(page)

        for step in ("format_json", "period_all_time", "download_to_device"):
            try:
                _click_by_accessible_names(page, step)
                page.wait_for_timeout(800)
            except SelectorError:
                pass

        pwd = page.locator('input[type="password"]')
        if pwd.count() > 0 and pwd.first.is_visible():
            return {
                "needs_password": True,
                "message": "Meta asks for your password; complete the export in the browser window.",
            }

        _click_by_accessible_names(page, "confirm_create")
        page.wait_for_timeout(2000)

        pwd = page.locator('input[type="password"]')
        if pwd.count() > 0 and pwd.first.is_visible():
            return {
                "needs_password": True,
                "message": "Meta asks for your password; complete the export in the browser window.",
            }

        return {
            "requested": True,
            "at": datetime.now(timezone.utc).isoformat(),
        }
    except SelectorError as exc:
        raise IgError(str(exc), step=exc.step, selectors_tried=exc.selectors_tried) from exc
    finally:
        _close(pw, context)


def download(url: str, out_dir: Optional[Path] = None) -> Dict[str, Any]:
    _require_playwright()
    target_dir = out_dir or DUMPS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    out_path = target_dir / f"{stamp}_ig_export.zip"

    pw, context, page = _launch_context(headless=True)
    try:
        with page.expect_download(timeout=120_000) as dl_info:
            page.goto(url, wait_until="domcontentloaded")
        download_obj = dl_info.value
        download_obj.save_as(str(out_path))
        size = out_path.stat().st_size
        return {"path": str(out_path), "size": size}
    except PlaywrightTimeoutError:
        response = context.request.get(url)
        if not response.ok:
            raise IgError(f"Download failed HTTP {response.status}", step="download") from None
        body = response.body()
        out_path.write_bytes(body)
        return {"path": str(out_path), "size": len(body)}
    except SelectorError as exc:
        raise IgError(str(exc), step=exc.step, selectors_tried=exc.selectors_tried) from exc
    finally:
        _close(pw, context)


def browse(url: str) -> Dict[str, Any]:
    _require_playwright()
    pw, context, page = _launch_context(headless=True)
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        status = response.status if response else 0
        title = page.title()
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
        if len(text) > TEXT_TRUNCATE:
            text = text[:TEXT_TRUNCATE]
        return {"status": status, "title": title, "text": text}
    finally:
        _close(pw, context)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Instagram export helper")
    parser.add_argument("command", choices=["login", "state", "request", "download", "browse"])
    parser.add_argument("--url", default="")
    return parser


def main() -> None:
    import sys

    args = build_parser().parse_args()
    try:
        if args.command == "login":
            print(json.dumps(login()))
        elif args.command == "state":
            print(json.dumps(state()))
        elif args.command == "request":
            print(json.dumps(request_export()))
        elif args.command == "download":
            if not args.url:
                print(json.dumps({"error": "url required"}))
                sys.exit(1)
            print(json.dumps(download(args.url)))
        elif args.command == "browse":
            if not args.url:
                print(json.dumps({"error": "url required"}))
                sys.exit(1)
            print(json.dumps(browse(args.url)))
    except IgError as exc:
        print(json.dumps({"error": str(exc), "step": exc.step}))
        sys.exit(1)


if __name__ == "__main__":
    main()
