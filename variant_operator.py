#!/usr/bin/env python3
"""
Variant operator: Playwright automation for https://variant.com/projects
VARIATIONS mode only: submit prompt, monitor streaming API for completion, export URL/screenshot per output (no HTML).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from playwright.sync_api import sync_playwright, Page, Response

from screenshot_stitch import capture_full_page_scrolled


# ----------------------------
# Reference image limits
# ----------------------------

MAX_REFERENCE_IMAGES = 2
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB


def validate_reference_images(image_paths: List[str]) -> List[str]:
    """Validate at most MAX_REFERENCE_IMAGES and each file < MAX_IMAGE_SIZE_BYTES."""
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
# Variant selectors & heuristics
# ----------------------------

VARIANT_AUTH_TEXTS = ["Sign in", "SIGN IN", "Sign in or sign up", "Log in"]
SEND_TEXTS = ["Send", "Submit"]

# Project URL: variant.com/chat/ or variant.com/projects/...
VARIANT_PROJECT_URL_PATTERN = re.compile(r"variant\.com/(chat|projects)/", re.I)
# Streaming API: GET https://variant.com/chats/<project>/streaming
VARIANT_STREAMING_URL_PATTERN = re.compile(r"variant\.com/chats/([^/]+)/streaming", re.I)


def extract_chat_id_from_url(url: str) -> Optional[str]:
    """Extract chat/project id from variant.com/chat/... or variant.com/projects/..."""
    if not url or "variant.com" not in url:
        return None
    # variant.com/chat/XYZ or variant.com/projects/XYZ
    m = re.search(r"variant\.com/(?:chat|projects)/([^/?&#]+)", url, re.I)
    return m.group(1).strip("/") if m else None


def is_on_project_chat_page(page: Page) -> bool:
    """True if we're on a project chat page (variant.com/chat/...). New projects redirect here after submit from /projects."""
    return "variant.com/chat/" in (page.url or "")


def page_has_auth_gate(page: Page) -> bool:
    """Look for Sign in / Sign in or sign up in body (button or link)."""
    body = page.locator("body")
    for t in VARIANT_AUTH_TEXTS:
        if body.get_by_role("link", name=re.compile(re.escape(t), re.I)).count() > 0:
            return True
        if body.get_by_role("button", name=re.compile(re.escape(t), re.I)).count() > 0:
            return True
    return False


def find_prompt_input(page: Page):
    """Find the main prompt input (Design anything... placeholder)."""
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
    """Click Send / Submit or press Enter to submit the prompt."""
    body = page.locator("body")
    for t in SEND_TEXTS:
        btn = body.get_by_role("button", name=re.compile(re.escape(t), re.I))
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


def get_output_labels_ordered(page: Page) -> List[str]:
    """
    Find all output cards (cards that have a Menu button) and return their label text in DOM order.
    Uses JS to walk from each Menu button to an ancestor and extract label (heading/title).
    """
    try:
        labels = page.evaluate("""
            () => {
                const menuSelectors = [
                    'button[aria-label*="Menu" i]',
                    'button[aria-label*="menu"]',
                    '[role="button"][aria-label*="Menu" i]',
                    '[data-testid*="menu" i]',
                ];
                let buttons = [];
                for (const sel of menuSelectors) {
                    try {
                        buttons = Array.from(document.querySelectorAll(sel));
                        if (buttons.length > 0) break;
                    } catch (_) {}
                }
                if (buttons.length === 0) {
                    const allButtons = document.querySelectorAll('button');
                    buttons = Array.from(allButtons).filter(b => {
                        const label = (b.getAttribute('aria-label') || b.textContent || '').trim();
                        return /menu|more|\\.\\.\\./i.test(label) || (b.textContent || '').trim() === '...';
                    });
                }
                const result = [];
                for (const btn of buttons) {
                    if (!btn.offsetParent) continue;
                    let el = btn.closest('a') || btn.closest('article') || btn.parentElement;
                    for (let i = 0; i < 8 && el; i++, el = el.parentElement) {
                        const heading = el.querySelector('h1, h2, h3, h4, [class*="title" i], [class*="label" i], [class*="name" i]');
                        const label = heading ? (heading.textContent || '').trim() : (el.textContent || '').trim().split('\\n')[0].slice(0, 120);
                        if (label && label.length > 0) {
                            result.push(label);
                            break;
                        }
                    }
                }
                return result;
            }
        """)
        if isinstance(labels, list):
            return [str(x).strip() for x in labels if x and str(x).strip()]
    except Exception:
        pass
    return []


def get_menu_buttons_in_grid(page: Page):
    """
    Return a locator that resolves to all Menu buttons in the card grid (output cards).
    Prefer scoping to main content to avoid sidebar menus.
    """
    main = page.locator("main")
    if main.count() > 0:
        return main.get_by_role("button", name=re.compile("Menu|more|\\.\\.\\.", re.I))
    return page.get_by_role("button", name=re.compile("Menu|more|\\.\\.\\.", re.I))


def wait_for_new_outputs(
    page: Page,
    existing_labels: Set[str],
    expected_count: int = 4,
    timeout_s: int = 300,
) -> Dict[str, Any]:
    """
    Poll until we have exactly expected_count new output cards (Menu + label not in existing_labels).
    Return telemetry and list of new labels in DOM order.
    """
    t0 = time.time()
    new_labels: List[str] = []
    while True:
        elapsed = time.time() - t0
        if elapsed > timeout_s:
            return {
                "done": False,
                "reason": "timeout",
                "elapsed_s": round(elapsed, 2),
                "new_labels": new_labels,
                "new_count": len(new_labels),
            }
        labels_ordered = get_output_labels_ordered(page)
        new_labels = [l for l in labels_ordered if l not in existing_labels]
        if len(new_labels) >= expected_count:
            return {
                "done": True,
                "reason": "new_outputs_ready",
                "elapsed_s": round(elapsed, 2),
                "new_labels": new_labels[:expected_count],
                "new_count": expected_count,
            }
        time.sleep(1.0)


def wait_for_project_url(page: Page, start_url: str, timeout_ms: int = 60_000) -> Optional[str]:
    """After submit, wait for URL to look like a project (variant.com/chat/ or variant.com/projects/)."""
    t0 = time.time()
    while (time.time() - t0) * 1000 < timeout_ms:
        url = (page.url or "").strip()
        if VARIANT_PROJECT_URL_PATTERN.search(url) and url != start_url.rstrip("/"):
            return url
        page.wait_for_timeout(1000)
    return None


# ----------------------------
# Streaming API monitoring
# ----------------------------

def _parse_streaming_response(body_bytes: bytes) -> Optional[Dict[str, Any]]:
    """Parse streaming API JSON; return None on failure."""
    try:
        return json.loads(body_bytes.decode("utf-8"))
    except Exception:
        return None


def _version_ids_from_cards(cards: List[Dict[str, Any]]) -> List[str]:
    """Extract versionId from each card's meta, preserving order."""
    ids: List[str] = []
    for c in cards or []:
        meta = c.get("meta") or {}
        vid = meta.get("versionId")
        if vid and isinstance(vid, str):
            ids.append(vid)
    return ids


def register_streaming_listener(
    page: Page,
    result_path: Path,
    meta_to_merge: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Register a response listener for GET variant.com/chats/*/streaming.
    Returns a state dict. Set state["chat_id"] when project URL is known, then call wait_for_streaming_complete.
    """
    state: Dict[str, Any] = {
        "chat_id": None,
        "version_ids": [],
        "last_active_response": None,
        "seen_active": False,
        "generation_complete": False,
        "result_path": result_path,
        "meta_to_merge": meta_to_merge,
    }

    def on_response(response: Response) -> None:
        url = response.url or ""
        if not VARIANT_STREAMING_URL_PATTERN.search(url):
            return
        if response.request.method != "GET":
            return
        try:
            body = response.body()
        except Exception:
            return
        data = _parse_streaming_response(body)
        if not data:
            return
        resp_chat_id = data.get("chatId")
        if not resp_chat_id:
            return
        cid = state.get("chat_id")
        if cid is None:
            state["chat_id"] = resp_chat_id
            cid = resp_chat_id
        if cid != resp_chat_id:
            return
        phase = (data.get("streamState") or {}).get("phase")
        cards = data.get("cards") or []

        if phase == "idle":
            if state["seen_active"]:
                state["generation_complete"] = True
            return
        if phase == "active":
            state["seen_active"] = True
            state["last_active_response"] = data
            version_ids = _version_ids_from_cards(cards)
            if version_ids:
                state["version_ids"] = version_ids
                out = {**state["meta_to_merge"], "version_ids": version_ids, "streaming_last_active": True}
                try:
                    dump_json(state["result_path"], out)
                except Exception:
                    pass

    page.on("response", on_response)
    return state


def wait_for_streaming_complete(
    state: Dict[str, Any],
    chat_id: str,
    timeout_s: int = 300,
    page: Optional[Page] = None,
) -> List[str]:
    """
    Set state["chat_id"] and poll until generation_complete or timeout.
    Returns versionIds from the last active streaming response before idle.
    """
    state["chat_id"] = chat_id
    t0 = time.time()
    while True:
        if state.get("generation_complete"):
            break
        if time.time() - t0 > timeout_s:
            break
        if page:
            page.wait_for_timeout(500)
        else:
            time.sleep(0.5)

    version_ids = state.get("version_ids") or []
    if state.get("last_active_response"):
        version_ids = _version_ids_from_cards(state["last_active_response"].get("cards") or [])
    return version_ids


def export_outputs_for_version_ids(
    page: Page,
    version_ids: List[str],
    captures_dir: Path,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    For each versionId: navigate to variant.com/shared/<versionId>, take full-page screenshot, save URL.
    Returns (urls_entries, capture_paths). No HTML export.
    """
    urls_entries: List[Dict[str, Any]] = []
    capture_paths: List[str] = []
    base_url = "https://variant.com/shared"
    for i, version_id in enumerate(version_ids):
        url = f"{base_url}/{version_id}"
        try:
            page.goto(url, wait_until="load", timeout=60_000)
            page.wait_for_timeout(1500)
            current_url = page.url or url
            ts = now_ms()
            capture_name = f"screenshot_{ts}.png"
            capture_path = captures_dir / capture_name
            capture_full_page_scrolled(page, capture_path)
            capture_paths.append(str(capture_path))
            urls_entries.append({
                "versionId": version_id,
                "url": current_url,
                "capture": f"captures/{capture_name}",
            })
        except Exception as e:
            urls_entries.append({
                "versionId": version_id,
                "url": url,
                "capture": "",
                "error": str(e),
            })
        if i < len(version_ids) - 1:
            page.wait_for_timeout(500)
    return (urls_entries, capture_paths)


# ----------------------------
# Core runner
# ----------------------------

@dataclass
class RunArgs:
    url: str
    prompt: str
    images: List[str]
    out_dir: Path
    headed: bool
    profile_dir: Optional[Path]
    connect_url: Optional[str]
    timeout_s: int


@dataclass
class ReexportArgs:
    """Args for re-export: read result.json, get version_ids, visit each shared URL and screenshot."""
    out_dir: Path
    headed: bool
    profile_dir: Optional[Path]
    connect_url: Optional[str]


def run_variant_reexport(args: ReexportArgs) -> Dict[str, Any]:
    """
    Re-export from existing result.json: read version_ids (and variant_project_url for reference),
    visit each variant.com/shared/<versionId>, take full-page screenshot, write urls.json.
    Does not open the project chat page or discover outputs from the DOM.
    """
    out_dir = Path(args.out_dir).resolve()
    result_path = out_dir / "result.json"
    if not result_path.exists():
        raise FileNotFoundError(
            f"No result.json at {result_path}. Run variant (run-variant) first for this step."
        )
    data = json.loads(result_path.read_text(encoding="utf-8"))
    version_ids = data.get("version_ids") or []
    if not version_ids:
        raise ValueError(
            f"No version_ids in {result_path}. Run variant (run-variant) first to generate outputs."
        )
    variant_project_url = data.get("variant_project_url")  # optional reference

    captures_dir = out_dir / "captures"
    urls_json_path = out_dir / "urls.json"
    captures_dir.mkdir(parents=True, exist_ok=True)

    meta: Dict[str, Any] = {
        "mode": "re_export",
        "out_dir": str(out_dir),
        "started_ms": now_ms(),
        "version_ids": version_ids,
    }
    if variant_project_url:
        meta["variant_project_url"] = variant_project_url

    with sync_playwright() as p:
        attached = args.connect_url is not None
        if attached:
            connect_url = (args.connect_url or "").strip()
            if "localhost" in connect_url:
                connect_url = connect_url.replace("localhost", "127.0.0.1")
            try:
                browser = p.chromium.connect_over_cdp(connect_url)
            except Exception as e:
                raise RuntimeError(
                    f"Could not connect to browser at {connect_url}: {e}. "
                    "Start Chrome with --remote-debugging-port=9222."
                ) from e
            if not browser.contexts:
                raise RuntimeError("No browser context found. Start Chrome with --remote-debugging-port=9222")
            context = browser.contexts[0]
            pages = context.pages
            page = pages[0] if pages else context.new_page()
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
            page = context.new_page()

        try:
            urls_entries, capture_paths = export_outputs_for_version_ids(
                page, version_ids, captures_dir
            )
            dump_json(urls_json_path, urls_entries)
            meta["finished_ms"] = now_ms()
            meta["capture_paths"] = capture_paths
            meta["urls_json_path"] = str(urls_json_path)
            meta["capture_path"] = capture_paths[0] if capture_paths else None
            # Update result.json with new capture paths / urls path
            data["capture_paths"] = capture_paths
            data["urls_json_path"] = str(urls_json_path)
            data["finished_ms"] = meta["finished_ms"]
            dump_json(result_path, data)
            return {
                "ok": True,
                "re_export": True,
                "capture_paths": capture_paths,
                "urls_json_path": str(urls_json_path),
                "version_ids": version_ids,
            }
        finally:
            if not attached:
                try:
                    context.close()
                except Exception:
                    pass


def run_variant_operator(args: RunArgs) -> Dict[str, Any]:
    ensure_dir(args.out_dir)
    captures_dir = args.out_dir / "captures"
    ensure_dir(captures_dir)

    prompt_used_path = args.out_dir / "prompt_used.txt"
    url_txt_path = args.out_dir / "url.txt"
    result_path = args.out_dir / "result.json"
    urls_json_path = args.out_dir / "urls.json"
    debug_html = args.out_dir / "debug.html"
    debug_png = args.out_dir / "debug.png"

    meta: Dict[str, Any] = {
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
            try:
                context.grant_permissions(["clipboard-read", "clipboard-write"])
            except Exception:
                pass
            pages = context.pages
            page = None
            for tab in pages:
                try:
                    u = tab.url or ""
                    if "variant.com" in u:
                        page = tab
                        if args.url.rstrip("/") in u:
                            break
                except Exception:
                    pass
            if page is None and pages:
                page = pages[0]
            if page is None:
                raise RuntimeError("No tabs found. Open a Variant tab and re-run with --connect.")
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

            prompt_used_path.write_text(args.prompt, encoding="utf-8")

            # Register streaming API listener before submit so we catch GET .../chats/<project>/streaming.
            streaming_state = register_streaming_listener(page, result_path, meta)

            # Find prompt input and fill
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
            try:
                page.evaluate("(t) => navigator.clipboard.writeText(t)", args.prompt)
            except Exception:
                pass
            page.wait_for_timeout(100)
            composer.press("Control+v")
            page.wait_for_timeout(200)

            if args.images:
                file_input = find_file_input(page)
                if file_input is None:
                    meta["attach_warning"] = "No file input found; images not attached."
                else:
                    file_input.set_input_files([str(Path(x).resolve()) for x in args.images])
                    page.wait_for_timeout(800)

            if not click_send(page):
                save_debug(page)
                raise RuntimeError("Could not submit prompt (Send/Submit failed).")

            # Wait for project URL (first run may redirect)
            variant_project_url: Optional[str] = None
            project_url = wait_for_project_url(page, args.url, timeout_ms=60_000)
            if project_url:
                variant_project_url = project_url
                url_txt_path.write_text(variant_project_url, encoding="utf-8")
                meta["variant_project_url"] = variant_project_url
            else:
                current = (page.url or "").strip()
                if VARIANT_PROJECT_URL_PATTERN.search(current):
                    variant_project_url = current
                    url_txt_path.write_text(variant_project_url, encoding="utf-8")
                    meta["variant_project_url"] = variant_project_url
            page.wait_for_timeout(2000)

            chat_id = extract_chat_id_from_url(variant_project_url or page.url or "")
            if not chat_id:
                save_debug(page)
                raise RuntimeError(
                    "Could not extract chat id from project URL for streaming monitor. "
                    f"URL: {variant_project_url or page.url}"
                )

            # Wait for generation to complete via streaming API (idle after active).
            version_ids = wait_for_streaming_complete(
                streaming_state, chat_id, timeout_s=args.timeout_s, page=page
            )
            if not version_ids:
                save_debug(page)
                raise RuntimeError(
                    f"No outputs from streaming API within {args.timeout_s}s. "
                    "Generation may have failed or timed out."
                )
            meta["version_ids"] = version_ids

            page.wait_for_timeout(1000)

            # Export: navigate to variant.com/shared/<versionId> for each, screenshot and save URL (no HTML).
            try:
                urls_entries, capture_paths = export_outputs_for_version_ids(
                    page, version_ids, captures_dir
                )
            except Exception as e:
                meta["export_error"] = str(e)
                save_debug(page)
                raise

            dump_json(urls_json_path, urls_entries)
            meta["finished_ms"] = now_ms()
            meta["prompt_used_path"] = str(prompt_used_path)
            meta["url_txt_path"] = str(url_txt_path)
            meta["capture_paths"] = capture_paths
            meta["urls_json_path"] = str(urls_json_path)
            dump_json(result_path, meta)

            result: Dict[str, Any] = {
                "ok": True,
                "prompt_used_path": str(prompt_used_path),
                "url_txt_path": str(url_txt_path),
                "capture_paths": capture_paths,
                "urls_json_path": str(urls_json_path),
                "version_ids": version_ids,
            }
            if variant_project_url is not None:
                result["variant_project_url"] = variant_project_url
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
    p = argparse.ArgumentParser(
        prog="variant_operator",
        description="Variant.com automation: VARIATIONS mode (submit prompt, monitor streaming API, export URL/screenshot per output).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="Run Variant in VARIATIONS mode.")
    run.add_argument("--url", required=True, help="Start URL or project URL (variant.com/projects or variant.com/chat/...).")
    run.add_argument("--prompt-file", required=True, help="Path to prompt (e.g. gpt/outputs/variant_prompt.txt).")
    run.add_argument("--image", action="append", default=[], help="Image path to attach (repeatable).")
    run.add_argument("--out", required=True, help="Output directory (generators/variant).")
    run.add_argument("--timeout-s", type=int, default=300, help="Timeout waiting for 4 new outputs.")
    run.add_argument("--headed", action="store_true", help="Run with visible browser.")
    run.add_argument("--profile-dir", default=None, help="Chrome profile for persistent login.")
    run.add_argument("--connect", default=None, metavar="URL", help="Attach to Chrome via CDP.")

    reexport = sub.add_parser(
        "re-export",
        help="Re-export from result.json: read version_ids, visit each shared URL, screenshot. No DOM discovery.",
    )
    reexport.add_argument("--out", required=True, help="Output directory containing result.json (e.g. generators/variant).")
    reexport.add_argument("--headed", action="store_true", help="Run with visible browser.")
    reexport.add_argument("--profile-dir", default=None, help="Chrome profile for persistent login.")
    reexport.add_argument("--connect", default=None, metavar="URL", help="Attach to Chrome via CDP.")

    return p


def main() -> None:
    parser = build_parser()
    ns = parser.parse_args()
    if ns.cmd == "run":
        prompt = read_text_file(Path(ns.prompt_file))
        out_dir = Path(ns.out).resolve()
        profile_dir = Path(ns.profile_dir).resolve() if ns.profile_dir else None
        connect_url = (ns.connect or "").strip() or None
        images = validate_reference_images(ns.image or [])
        rargs = RunArgs(
            url=ns.url,
            prompt=prompt,
            images=images,
            out_dir=out_dir,
            headed=bool(ns.headed),
            profile_dir=profile_dir,
            connect_url=connect_url,
            timeout_s=int(ns.timeout_s),
        )
        result = run_variant_operator(rargs)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif ns.cmd == "re-export":
        out_dir = Path(ns.out).resolve()
        profile_dir = Path(ns.profile_dir).resolve() if ns.profile_dir else None
        connect_url = (ns.connect or "").strip() or None
        rargs = ReexportArgs(
            out_dir=out_dir,
            headed=bool(ns.headed),
            profile_dir=profile_dir,
            connect_url=connect_url,
        )
        result = run_variant_reexport(rargs)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        return


if __name__ == "__main__":
    main()
