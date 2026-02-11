#!/usr/bin/env python3
"""
Aura.build operator: Playwright automation for https://www.aura.build/
Supports DNA mode (new project from aura_dna.txt) and FEEDBACK mode (edit existing project with aura_edit.txt).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.sync_api import sync_playwright, Page


# ----------------------------
# Reference image limits
# ----------------------------

MAX_REFERENCE_IMAGES = 2
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB


def validate_reference_images(image_paths: List[str]) -> List[str]:
    """
    Validate at most MAX_REFERENCE_IMAGES and each file < MAX_IMAGE_SIZE_BYTES.
    Returns the list to use (first MAX_REFERENCE_IMAGES). Raises on violation.
    """
    if not image_paths:
        return []
    if len(image_paths) > MAX_REFERENCE_IMAGES:
        raise ValueError(
            f"At most {MAX_REFERENCE_IMAGES} reference images allowed, got {len(image_paths)}."
        )
    out: List[str] = []
    for p in image_paths:
        path = Path(p).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Reference image not found: {path}")
        size = path.stat().st_size
        if size >= MAX_IMAGE_SIZE_BYTES:
            raise ValueError(
                f"Reference image too large: {path.name} ({size / (1024*1024):.2f} MB). "
                f"Maximum size is {MAX_IMAGE_SIZE_BYTES // (1024*1024)} MB."
            )
        out.append(str(path))
    return out[:MAX_REFERENCE_IMAGES]


# ----------------------------
# Utilities
# ----------------------------

def read_text_file(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def now_ms() -> int:
    return int(time.time() * 1000)


def dump_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ----------------------------
# Aura selectors & heuristics
# ----------------------------

AURA_AUTH_TEXTS = ["Sign in", "SIGN IN", "Log in"]
SEND_TEXTS = ["Send", "Submit"]
GENERATING_TEXT = "Generating code..."
EXPORT_TEXT = "Export"
COPY_HTML_TEXT = "Copy HTML"
HIDE_SIDEBAR_TEXT = "Hide sidebar"
SHOW_SIDEBAR_TEXT = "Show sidebar"

# URL pattern for editor redirect (e.g. https://www.aura.build/editor/xxx or aura.build/editor/<id>)
AURA_EDITOR_URL_PATTERN = re.compile(r"aura\.build/editor/", re.I)


def page_has_auth_gate(page: Page) -> bool:
    """Look for Sign in / SIGN IN in nav (button or link)."""
    body = page.locator("body")
    for t in AURA_AUTH_TEXTS:
        if body.get_by_role("link", name=re.compile(re.escape(t), re.I)).count() > 0:
            return True
        if body.get_by_role("button", name=re.compile(re.escape(t), re.I)).count() > 0:
            return True
    return False


def find_prompt_input(page: Page):
    """Find the main prompt input (textarea or contenteditable)."""
    candidates = [
        page.locator("textarea"),
        page.get_by_role("textbox"),
        page.locator("[contenteditable='true']"),
    ]
    for c in candidates:
        if c.count() > 0:
            for i in range(min(c.count(), 6)):
                el = c.nth(i)
                try:
                    if el.is_visible():
                        return el
                except Exception:
                    pass
    return None


def find_file_input(page: Page):
    """File input for image attachments."""
    candidates = [
        page.locator("input[type='file'][accept*='image']"),
        page.locator("input[type='file']"),
    ]
    for c in candidates:
        if c.count() > 0:
            for i in range(min(c.count(), 6)):
                el = c.nth(i)
                try:
                    if el.is_visible():
                        return el
                except Exception:
                    pass
            return c.first
    return None


def click_send(page: Page) -> bool:
    """Click Send/Submit to submit the prompt."""
    body = page.locator("body")
    for t in SEND_TEXTS:
        btn = body.get_by_role("button", name=re.compile(t, re.I))
        if btn.count() > 0:
            try:
                btn.first.click(timeout=2000)
                return True
            except Exception:
                pass
    sendish = page.locator("button[aria-label*='Send'], button[aria-label*='send']")
    if sendish.count() > 0:
        try:
            sendish.first.click(timeout=2000)
            return True
        except Exception:
            pass
    inp = find_prompt_input(page)
    if inp is not None:
        try:
            inp.press("Enter")
            return True
        except Exception:
            pass
    return False


def generating_code_visible(page: Page) -> bool:
    """True if 'Generating code...' text is visible in the left sidebar / page."""
    try:
        loc = page.get_by_text(GENERATING_TEXT, exact=False)
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


def wait_until_generating_done(page: Page, timeout_s: int = 150) -> Dict[str, Any]:
    """Poll until 'Generating code...' disappears in the left sidebar. Return telemetry."""
    t0 = time.time()
    seen_generating = False
    while True:
        elapsed = time.time() - t0
        if elapsed > timeout_s:
            return {"done": False, "reason": "timeout", "elapsed_s": round(elapsed, 2), "seen_generating": seen_generating}
        visible = generating_code_visible(page)
        if visible:
            seen_generating = True
        if seen_generating and not visible:
            return {"done": True, "reason": "generating_disappeared", "elapsed_s": round(elapsed, 2), "seen_generating": seen_generating}
        time.sleep(0.75)


def click_export_copy_html(page: Page) -> Optional[str]:
    """Click Export -> Copy HTML in navbar, return HTML from clipboard."""
    body = page.locator("body")
    # Find Export button
    export_btn = body.get_by_role("button", name=re.compile(EXPORT_TEXT, re.I))
    if export_btn.count() == 0:
        export_btn = body.get_by_text(EXPORT_TEXT, exact=False)
    if export_btn.count() == 0:
        return None
    try:
        export_btn.first.click(timeout=3000)
    except Exception:
        return None
    page.wait_for_timeout(500)
    # In dropdown, click "Copy HTML"
    copy_html = body.get_by_role("button", name=re.compile(COPY_HTML_TEXT, re.I))
    if copy_html.count() == 0:
        copy_html = body.get_by_text(COPY_HTML_TEXT, exact=False)
    if copy_html.count() == 0:
        return None
    try:
        copy_html.first.click(timeout=2000)
    except Exception:
        return None
    page.wait_for_timeout(500)
    try:
        txt = page.evaluate("() => navigator.clipboard.readText()")
        if isinstance(txt, str) and txt.strip():
            return txt
    except Exception:
        pass
    return None


def sidebar_toggle(page: Page, hide: bool) -> bool:
    """Click 'Hide sidebar' (hide=True) or 'Show sidebar' (hide=False)."""
    body = page.locator("body")
    text = HIDE_SIDEBAR_TEXT if hide else SHOW_SIDEBAR_TEXT
    btn = body.get_by_role("button", name=re.compile(re.escape(text), re.I))
    if btn.count() == 0:
        btn = body.get_by_text(text, exact=False)
    if btn.count() == 0:
        return False
    try:
        btn.first.click(timeout=2000)
        return True
    except Exception:
        return False


def ensure_sidebar_visible(page: Page) -> bool:
    """Ensure chat sidebar is visible: if 'Show sidebar' is visible, click it so we see 'Hide sidebar'."""
    body = page.locator("body")
    show_btn = body.get_by_role("button", name=re.compile(re.escape(SHOW_SIDEBAR_TEXT), re.I))
    if show_btn.count() == 0:
        show_btn = body.get_by_text(SHOW_SIDEBAR_TEXT, exact=False)
    if show_btn.count() > 0:
        try:
            if show_btn.first.is_visible():
                show_btn.first.click(timeout=2000)
                page.wait_for_timeout(500)
                return True
        except Exception:
            pass
    return True  # Already visible or no button


def wait_for_editor_redirect(page: Page, timeout_ms: int = 60_000) -> Optional[str]:
    """After DNA submit, wait for URL to contain aura.build/editor/<id>. Return final URL or None."""
    t0 = time.time()
    while (time.time() - t0) * 1000 < timeout_ms:
        url = (page.url or "").strip()
        if AURA_EDITOR_URL_PATTERN.search(url):
            return url
        page.wait_for_timeout(1000)
    return None


# ----------------------------
# Core runner
# ----------------------------

@dataclass
class RunArgs:
    mode: str  # DNA | FEEDBACK
    url: str
    prompt: str
    images: List[str]
    out_dir: Path
    headed: bool
    profile_dir: Optional[Path]
    connect_url: Optional[str]
    timeout_s: int


def run_aura_operator(args: RunArgs) -> Dict[str, Any]:
    ensure_dir(args.out_dir)
    exports_dir = args.out_dir / "exports"
    captures_dir = args.out_dir / "captures"
    ensure_dir(exports_dir)
    ensure_dir(captures_dir)

    prompt_used_path = args.out_dir / "prompt_used.txt"
    url_txt_path = args.out_dir / "url.txt"
    result_path = args.out_dir / "result.json"
    debug_html = args.out_dir / "debug.html"
    debug_png = args.out_dir / "debug.png"

    meta: Dict[str, Any] = {
        "mode": args.mode,
        "url": args.url,
        "out_dir": str(args.out_dir),
        "started_ms": now_ms(),
    }

    def save_debug(page: Page) -> None:
        try:
            debug_html.write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(debug_png), full_page=True)
        except Exception:
            pass

    with sync_playwright() as p:
        attached = args.connect_url is not None
        if attached:
            connect_url = args.connect_url.strip()
            if "localhost" in connect_url:
                connect_url = connect_url.replace("localhost", "127.0.0.1")
            try:
                browser = p.chromium.connect_over_cdp(connect_url)
            except Exception as e:
                raise RuntimeError(
                    f"Could not connect to browser at {connect_url}: {e}. "
                    "Start Chrome with: chrome.exe --remote-debugging-port=9222 "
                    "then run this script again with --connect http://127.0.0.1:9222"
                ) from e
            if not browser.contexts:
                raise RuntimeError("No browser context found. Start Chrome with --remote-debugging-port=9222")
            context = browser.contexts[0]
            pages = context.pages
            page = None
            for tab in pages:
                try:
                    u = tab.url or ""
                    if "aura.build" in u:
                        page = tab
                        if args.url.rstrip("/") in u:
                            break
                except Exception:
                    pass
            if page is None and pages:
                page = pages[0]
            if page is None:
                raise RuntimeError("No tabs found. Open an Aura tab and re-run with --connect.")
            if args.url.rstrip("/") not in (page.url or ""):
                page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(1500)
        else:
            browser = p.chromium.launch(headless=not args.headed)
            if args.profile_dir is not None:
                ensure_dir(args.profile_dir)
                browser.close()
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(args.profile_dir),
                    headless=not args.headed,
                )
            else:
                context = browser.new_context()
            if not attached:
                try:
                    context.grant_permissions(["clipboard-read", "clipboard-write"])
                except Exception:
                    pass
            page = context.new_page()
            page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(1500)

        try:
            if page_has_auth_gate(page):
                if args.profile_dir is not None and args.headed:
                    login_timeout_s = 300
                    print(
                        "Auth required. Please log in in the browser window. "
                        f"Waiting up to {login_timeout_s}s for you to complete login...",
                        file=sys.stderr,
                    )
                    t0 = time.time()
                    while time.time() - t0 < login_timeout_s:
                        time.sleep(2)
                        if not page_has_auth_gate(page):
                            print("Login detected. Continuing...", file=sys.stderr)
                            page.wait_for_timeout(2000)
                            break
                    else:
                        save_debug(page)
                        raise RuntimeError("Login timeout. Log in in the browser and re-run, or increase wait time.")
                else:
                    save_debug(page)
                    if attached:
                        raise RuntimeError(
                            "Auth required (Sign in detected). Log in in that browser tab and re-run with --connect."
                        )
                    raise RuntimeError(
                        "Auth required (Sign in detected). "
                        "Run with --profile-dir and --headed, or use --connect with an already-logged-in Chrome."
                    )

            # Save prompt used (before submit)
            prompt_used_path.write_text(args.prompt, encoding="utf-8")

            # FEEDBACK: ensure sidebar is visible (Show sidebar -> click so we see Hide sidebar)
            if args.mode == "FEEDBACK":
                ensure_sidebar_visible(page)
                page.wait_for_timeout(300)

            # Find prompt input and fill (use clipboard paste so full multiline text is inserted without Enter triggering submit)
            composer = find_prompt_input(page)
            if composer is None:
                save_debug(page)
                raise RuntimeError("Could not find prompt input (textarea/textbox/contenteditable).")
            try:
                composer.click(timeout=3000)
            except Exception:
                pass
            try:
                composer.fill("")
            except Exception:
                try:
                    composer.press("Control+A")
                    composer.press("Backspace")
                except Exception:
                    pass
            # Paste full prompt via clipboard so newlines don't trigger submit
            try:
                page.evaluate("(t) => navigator.clipboard.writeText(t)", args.prompt)
            except Exception:
                pass
            page.wait_for_timeout(100)
            composer.press("Control+v")
            page.wait_for_timeout(200)

            # Attach images
            if args.images:
                file_input = find_file_input(page)
                if file_input is None:
                    meta["attach_warning"] = "No file input found; images not attached."
                else:
                    file_input.set_input_files([str(Path(x).resolve()) for x in args.images])
                    page.wait_for_timeout(800)

            # Submit
            if not click_send(page):
                save_debug(page)
                raise RuntimeError("Could not submit prompt (Send/Submit failed).")

            # DNA: wait for redirect to editor URL, then save URL
            aura_project_url: Optional[str] = None
            if args.mode == "DNA":
                aura_project_url = wait_for_editor_redirect(page, timeout_ms=60_000)
                if aura_project_url:
                    url_txt_path.write_text(aura_project_url, encoding="utf-8")
                    meta["aura_project_url"] = aura_project_url
                else:
                    # Maybe already on editor; use current URL if it matches
                    current = (page.url or "").strip()
                    if AURA_EDITOR_URL_PATTERN.search(current):
                        aura_project_url = current
                        url_txt_path.write_text(aura_project_url, encoding="utf-8")
                        meta["aura_project_url"] = aura_project_url
                page.wait_for_timeout(2000)

            # Wait until "Generating code..." disappears
            done_info = wait_until_generating_done(page, timeout_s=args.timeout_s)
            meta["done_info"] = done_info
            if not done_info.get("done"):
                save_debug(page)
                raise RuntimeError(f"Generation did not complete within {args.timeout_s}s (timeout).")

            # Aura sometimes reloads the page to render the final preview; wait for it to settle
            page.wait_for_timeout(5000)

            # Export -> Copy HTML
            html_content = click_export_copy_html(page)
            if not html_content:
                save_debug(page)
                raise RuntimeError("Could not get HTML from Export -> Copy HTML.")
            export_name = f"export_{now_ms()}.html"
            export_path = exports_dir / export_name
            export_path.write_text(html_content, encoding="utf-8")
            meta["export_path"] = str(export_path)

            # Hide sidebar -> full page screenshot -> Show sidebar
            if not sidebar_toggle(page, hide=True):
                meta["sidebar_warning"] = "Could not click Hide sidebar"
            page.wait_for_timeout(500)
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            page.wait_for_timeout(500)
            capture_name = f"screenshot_{now_ms()}.png"
            capture_path = captures_dir / capture_name
            page.screenshot(path=str(capture_path), full_page=True)
            meta["capture_path"] = str(capture_path)
            sidebar_toggle(page, hide=False)

            meta["finished_ms"] = now_ms()
            meta["prompt_used_path"] = str(prompt_used_path)
            meta["url_txt_path"] = str(url_txt_path)
            dump_json(result_path, meta)

            result: Dict[str, Any] = {
                "ok": True,
                "prompt_used_path": str(prompt_used_path),
                "url_txt_path": str(url_txt_path),
                "export_path": str(export_path),
                "capture_path": str(capture_path),
                "done_info": done_info,
            }
            if aura_project_url is not None:
                result["aura_project_url"] = aura_project_url
            return result

        except Exception as e:
            meta["error"] = str(e)
            meta["finished_ms"] = now_ms()
            try:
                dump_json(result_path, meta)
            except Exception:
                pass
            raise
        finally:
            if not attached:
                try:
                    context.close()
                except Exception:
                    pass


# ----------------------------
# CLI
# ----------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aura_operator", description="Aura.build automation: DNA and FEEDBACK modes.")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="Run Aura in DNA or FEEDBACK mode.")
    run.add_argument("--mode", choices=["DNA", "FEEDBACK"], required=True, help="DNA (new project) or FEEDBACK (edit).")
    run.add_argument("--url", required=True, help="Start URL (DNA) or project URL (FEEDBACK).")
    run.add_argument("--prompt-file", required=True, help="Path to prompt text (aura_dna.txt or aura_edit.txt).")
    run.add_argument("--image", action="append", default=[], help="Image path to attach (repeatable).")
    run.add_argument("--out", required=True, help="Output directory (generators/aura).")
    run.add_argument("--timeout-s", type=int, default=150, help="Timeout waiting for generation.")
    run.add_argument("--headed", action="store_true", help="Run with visible browser.")
    run.add_argument("--profile-dir", default=None, help="Chrome profile for persistent login.")
    run.add_argument("--connect", default=None, metavar="URL", help="Attach to Chrome via CDP.")
    return p


def main() -> None:
    parser = build_parser()
    ns = parser.parse_args()
    if ns.cmd != "run":
        return
    prompt = read_text_file(Path(ns.prompt_file))
    out_dir = Path(ns.out).resolve()
    profile_dir = Path(ns.profile_dir).resolve() if ns.profile_dir else None
    connect_url = (ns.connect or "").strip() or None
    images = validate_reference_images(ns.image or [])
    rargs = RunArgs(
        mode=ns.mode,
        url=ns.url,
        prompt=prompt,
        images=images,
        out_dir=out_dir,
        headed=bool(ns.headed),
        profile_dir=profile_dir,
        connect_url=connect_url,
        timeout_s=int(ns.timeout_s),
    )
    result = run_aura_operator(rargs)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
