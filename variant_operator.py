#!/usr/bin/env python3
"""
Variant operator: Playwright automation for https://variant.com/projects
VARIATIONS mode only: submit prompt, wait for 4 new output cards, export HTML/URL/screenshot per output.
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

from playwright.sync_api import sync_playwright, Page, BrowserContext


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
COPY_CODE_TEXTS = ["Copy code", "Copy Code"]
OPEN_IN_NEW_TAB_TEXTS = ["Open in new tab", "Open in new tab"]

# Project URL: variant.com/chat/ or variant.com/projects/...
VARIANT_PROJECT_URL_PATTERN = re.compile(r"variant\.com/(chat|projects)/", re.I)


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


def click_menu_copy_code(page: Page) -> Optional[str]:
    """Open Menu (must be visible), click 'Copy code', return HTML from clipboard."""
    body = page.locator("body")
    for t in COPY_CODE_TEXTS:
        item = body.get_by_role("menuitem", name=re.compile(re.escape(t), re.I))
        if item.count() == 0:
            item = body.get_by_role("button", name=re.compile(re.escape(t), re.I))
        if item.count() == 0:
            item = body.get_by_text(t, exact=False)
        if item.count() > 0:
            try:
                item.first.click(timeout=3000)
                page.wait_for_timeout(600)
                txt = page.evaluate("() => navigator.clipboard.readText()")
                if isinstance(txt, str) and txt.strip():
                    return txt
            except Exception:
                pass
    return None


def click_menu_open_in_new_tab(page: Page) -> bool:
    """Click 'Open in new tab' in the open menu. Returns True if clicked."""
    body = page.locator("body")
    for t in OPEN_IN_NEW_TAB_TEXTS:
        item = body.get_by_role("menuitem", name=re.compile(re.escape(t), re.I))
        if item.count() == 0:
            item = body.get_by_role("link", name=re.compile(re.escape(t), re.I))
        if item.count() == 0:
            item = body.get_by_role("button", name=re.compile(re.escape(t), re.I))
        if item.count() == 0:
            item = body.get_by_text(t, exact=False)
        if item.count() > 0:
            try:
                item.first.click(timeout=3000)
                return True
            except Exception:
                pass
    return False


def wait_for_project_url(page: Page, start_url: str, timeout_ms: int = 60_000) -> Optional[str]:
    """After submit, wait for URL to look like a project (variant.com/chat/ or variant.com/projects/)."""
    t0 = time.time()
    while (time.time() - t0) * 1000 < timeout_ms:
        url = (page.url or "").strip()
        if VARIANT_PROJECT_URL_PATTERN.search(url) and url != start_url.rstrip("/"):
            return url
        page.wait_for_timeout(1000)
    return None


def get_last_output_card_bottom(page: Page) -> Optional[float]:
    """
    Get the bottom Y (document coordinates) of the last output card in the grid.
    Used to cap scrolling so we never scroll past it (Variant would trigger new generation).
    """
    try:
        result = page.evaluate("""
            () => {
                const sel = 'button[aria-label*="Menu" i], button[aria-label*="menu"]';
                let buttons = Array.from(document.querySelectorAll(sel));
                if (buttons.length === 0)
                    buttons = Array.from(document.querySelectorAll('button'))
                        .filter(b => /menu|more|\\.\\.\\./i.test((b.getAttribute('aria-label') || b.textContent || '').trim()));
                if (buttons.length === 0) return null;
                const lastBtn = buttons[buttons.length - 1];
                let el = lastBtn.closest('a') || lastBtn.closest('article') || lastBtn.parentElement;
                for (let i = 0; i < 10 && el; i++, el = el.parentElement) {
                    const r = el.getBoundingClientRect();
                    if (r.height > 0) return r.bottom + window.scrollY;
                }
                return null;
            }
        """)
        if result is not None and isinstance(result, (int, float)):
            return float(result)
    except Exception:
        pass
    return None


def clamp_scroll_to_last_output(page: Page, last_card_bottom: Optional[float]) -> None:
    """
    If the page has scrolled past the last output card's bottom, scroll back up so we stay at or above it.
    Prevents Variant from triggering a new generation when scrolling too far.
    """
    if last_card_bottom is None:
        return
    try:
        page.evaluate("""
            (lastBottom) => {
                const maxScroll = Math.max(0, lastBottom - window.innerHeight);
                if (window.scrollY > maxScroll) window.scrollTo(0, maxScroll);
            }
        """, last_card_bottom)
    except Exception:
        pass


def _get_menu_buttons_list_js() -> str:
    """JS fragment that defines `buttons` (list of menu buttons, same order as get_output_labels_ordered)."""
    return """
        const sel = 'button[aria-label*="Menu" i], button[aria-label*="menu"]';
        let buttons = Array.from(document.querySelectorAll(sel));
        if (buttons.length === 0)
            buttons = Array.from(document.querySelectorAll('button'))
                .filter(b => /menu|more|\\.\\.\\./i.test((b.getAttribute('aria-label') || b.textContent || '').trim()));
    """


def click_output_card_by_index(page: Page, index: int) -> None:
    """
    Click the output card at the given index (same order as get_output_labels_ordered).
    Opens that output in preview/full view. Uses card = closest('a')||closest('article')||parent.
    """
    page.evaluate(
        "(idx) => { "
        + _get_menu_buttons_list_js()
        + " if (idx < 0 || idx >= buttons.length) return; const btn = buttons[idx]; "
        "const card = btn.closest('a') || btn.closest('article') || btn.parentElement; "
        "if (card) card.click(); }",
        index,
    )


def wait_for_preview(page: Page, timeout_ms: int = 15_000) -> None:
    """
    After clicking a card, wait until we are in preview: Export button visible or second design-name in header.
    """
    t0 = time.time()
    while (time.time() - t0) * 1000 < timeout_ms:
        export_btn = page.get_by_role("button", name=re.compile("Export", re.I))
        if export_btn.count() > 0 and export_btn.first.is_visible():
            page.wait_for_timeout(500)
            return
        design_names = page.locator("[class*='ChatTopbar'][class*='header'] [class*='design-name'], [class*='ChatTopbar_header'] [class*='design-name']")
        if design_names.count() >= 2:
            page.wait_for_timeout(500)
            return
        page.wait_for_timeout(300)
    raise RuntimeError("Timeout waiting for preview (Export button or design-name).")


def click_navbar_export(page: Page) -> bool:
    """Click the Export button in the navbar (preview view). Opens dropdown with Copy code / Open in new tab."""
    for sel in [
        page.get_by_role("button", name=re.compile("Export", re.I)),
        page.get_by_text("Export", exact=False),
    ]:
        if sel.count() > 0:
            try:
                sel.first.click(timeout=3000)
                page.wait_for_timeout(500)
                return True
            except Exception:
                pass
    return False


def scroll_output_card_into_view_and_clamp(page: Page, index: int, last_card_bottom: Optional[float]) -> None:
    """
    Scroll the output card at the given index into view (center it), then clamp scroll to last output.
    Uses same card resolution as click (closest a/article/parent).
    """
    try:
        page.evaluate(
            "(idx) => { "
            + _get_menu_buttons_list_js()
            + " if (idx < 0 || idx >= buttons.length) return; const btn = buttons[idx]; "
            "const card = btn.closest('a') || btn.closest('article') || btn.parentElement; "
            "if (card) card.scrollIntoView({ block: 'center', behavior: 'instant' }); }",
            index,
        )
        page.wait_for_timeout(300)
    except Exception:
        pass
    clamp_scroll_to_last_output(page, last_card_bottom)
    page.wait_for_timeout(200)


def scroll_card_into_view_and_clamp(page: Page, btn_locator, last_card_bottom: Optional[float]) -> None:
    """
    Scroll the card (via its menu button) into view so it is fully visible for hover/Menu,
    then clamp scroll so we never go past the last output card (avoids triggering new generation).
    """
    btn_locator.scroll_into_view_if_needed(timeout=3000)
    page.wait_for_timeout(300)
    # Center the card in the viewport so the full card (and Menu) is visible, then clamp.
    try:
        box = btn_locator.bounding_box()
        vh = (page.viewport_size or {}).get("height") or 600
        if box:
            delta_y = (vh / 2) - (box["y"] + box["height"] / 2)
            page.evaluate("""
                (deltaY) => {
                    const maxScroll = document.documentElement.scrollHeight - window.innerHeight;
                    const newScrollY = Math.max(0, Math.min(window.scrollY + deltaY, maxScroll));
                    window.scrollTo(0, newScrollY);
                }
            """, delta_y)
            page.wait_for_timeout(200)
    except Exception:
        pass
    clamp_scroll_to_last_output(page, last_card_bottom)
    page.wait_for_timeout(200)


def export_outputs_for_labels(
    page: Page,
    context: BrowserContext,
    labels: List[str],
    exports_dir: Path,
    captures_dir: Path,
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """
    For each output: click card to open preview, click Export in navbar, Copy code + Open in new tab,
    then go back to grid (scroll unchanged). Tracks exported_labels so we never process the same output twice.
    Returns (urls_entries, export_paths, capture_paths).
    """
    urls_entries: List[Dict[str, Any]] = []
    export_paths: List[str] = []
    capture_paths: List[str] = []
    exported_labels: Set[str] = set()
    labels_wanted = set(labels)
    last_card_bottom = get_last_output_card_bottom(page)

    while True:
        labels_ordered = get_output_labels_ordered(page)
        if not labels_ordered:
            break
        next_index = None
        for i, lab in enumerate(labels_ordered):
            if lab in labels_wanted and lab not in exported_labels:
                next_index = i
                break
        if next_index is None:
            break
        label = labels_ordered[next_index]

        scroll_output_card_into_view_and_clamp(page, next_index, last_card_bottom)
        click_output_card_by_index(page, next_index)
        page.wait_for_timeout(800)
        wait_for_preview(page)
        page.wait_for_timeout(300)

        if not click_navbar_export(page):
            page.go_back()
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            page.wait_for_timeout(500)
            raise RuntimeError("Could not click Export in navbar (preview view).")
        page.wait_for_timeout(400)

        html_content = click_menu_copy_code(page)
        ts = now_ms()
        export_name = f"export_{ts}.html"
        export_path = exports_dir / export_name
        if html_content:
            export_path.write_text(html_content, encoding="utf-8")
        export_paths.append(str(export_path))

        with context.expect_page(timeout=10000) as new_page_info:
            opened = click_menu_open_in_new_tab(page)
            if not opened:
                if click_navbar_export(page):
                    page.wait_for_timeout(400)
                    opened = click_menu_open_in_new_tab(page)
        try:
            new_page = new_page_info.value
        except Exception:
            new_page = None
        if new_page:
            new_page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1000)
            new_url = new_page.url or ""
            capture_name = f"screenshot_{ts}.png"
            capture_path = captures_dir / capture_name
            try:
                new_page.screenshot(path=str(capture_path), full_page=True)
            except Exception:
                pass
            capture_paths.append(str(capture_path))
            urls_entries.append({
                "label": label,
                "url": new_url,
                "export_html": f"exports/{export_name}",
                "capture": f"captures/{capture_name}",
            })
            new_page.close()
            page.wait_for_timeout(300)
        else:
            urls_entries.append({
                "label": label,
                "url": "",
                "export_html": f"exports/{export_name}",
                "capture": "",
            })

        exported_labels.add(label)
        page.go_back()
        page.wait_for_load_state("domcontentloaded", timeout=10000)
        page.wait_for_timeout(500)

    return (urls_entries, export_paths, capture_paths)


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
class ExportOnlyArgs:
    url: str
    out_dir: Path
    headed: bool
    profile_dir: Optional[Path]
    connect_url: Optional[str]


def run_variant_operator(args: RunArgs) -> Dict[str, Any]:
    ensure_dir(args.out_dir)
    exports_dir = args.out_dir / "exports"
    captures_dir = args.out_dir / "captures"
    ensure_dir(exports_dir)
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

            # Snapshot existing output labels only when already on a project chat page.
            # New project: we're on variant.com/projects; submit will redirect to variant.com/chat/<> — snapshot after that page loads.
            already_on_chat = is_on_project_chat_page(page)
            existing_labels: Set[str] = set(get_output_labels_ordered(page)) if already_on_chat else set()
            meta["existing_labels_count"] = len(existing_labels)
            meta["snapshot_after_redirect"] = not already_on_chat

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

            # New project: we just landed on variant.com/chat/<> — record output labels now (after chat page has loaded).
            if meta.get("snapshot_after_redirect"):
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                page.wait_for_timeout(1500)
                existing_labels = set(get_output_labels_ordered(page))
                meta["existing_labels_count"] = len(existing_labels)

            # Wait for 4 new output cards
            done_info = wait_for_new_outputs(page, existing_labels, expected_count=4, timeout_s=args.timeout_s)
            meta["done_info"] = done_info
            if not done_info.get("done"):
                save_debug(page)
                raise RuntimeError(
                    f"Did not get 4 new outputs within {args.timeout_s}s (got {done_info.get('new_count', 0)})."
                )

            new_labels: List[str] = done_info.get("new_labels") or []
            if len(new_labels) < 4:
                new_labels = (new_labels + [f"output_{i}" for i in range(4 - len(new_labels))])[:4]

            page.wait_for_timeout(3000)

            # Export each new output via shared helper
            try:
                urls_entries, export_paths, capture_paths = export_outputs_for_labels(
                    page, context, new_labels, exports_dir, captures_dir
                )
            except Exception as e:
                meta["export_error"] = str(e)
                save_debug(page)
                raise

            dump_json(urls_json_path, urls_entries)
            meta["finished_ms"] = now_ms()
            meta["prompt_used_path"] = str(prompt_used_path)
            meta["url_txt_path"] = str(url_txt_path)
            meta["export_paths"] = export_paths
            meta["capture_paths"] = capture_paths
            meta["urls_json_path"] = str(urls_json_path)
            dump_json(result_path, meta)

            result: Dict[str, Any] = {
                "ok": True,
                "prompt_used_path": str(prompt_used_path),
                "url_txt_path": str(url_txt_path),
                "export_paths": export_paths,
                "capture_paths": capture_paths,
                "urls_json_path": str(urls_json_path),
                "done_info": done_info,
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


def run_variant_export_only(args: ExportOnlyArgs) -> Dict[str, Any]:
    """
    Reload the project at the given URL and export all existing outputs (links, screenshots, HTML)
    without triggering a new generation.
    """
    ensure_dir(args.out_dir)
    exports_dir = args.out_dir / "exports"
    captures_dir = args.out_dir / "captures"
    ensure_dir(exports_dir)
    ensure_dir(captures_dir)

    result_path = args.out_dir / "result.json"
    urls_json_path = args.out_dir / "urls.json"
    debug_html = args.out_dir / "debug.html"
    debug_png = args.out_dir / "debug.png"

    meta: Dict[str, Any] = {
        "mode": "export_only",
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
                save_debug(page)
                if attached:
                    raise RuntimeError(
                        "Auth required (Sign in detected). Log in in that browser tab and re-run with --connect."
                    )
                raise RuntimeError(
                    "Auth required (Sign in detected). "
                    "Run with --profile-dir and --headed, or use --connect with an already-logged-in Chrome."
                )

            if not is_on_project_chat_page(page):
                save_debug(page)
                raise RuntimeError(
                    f"Not on a project chat page (expected variant.com/chat/...). Current URL: {page.url}"
                )

            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)
            all_labels = get_output_labels_ordered(page)
            meta["outputs_count"] = len(all_labels)

            if not all_labels:
                dump_json(urls_json_path, [])
                meta["finished_ms"] = now_ms()
                meta["export_paths"] = []
                meta["capture_paths"] = []
                meta["urls_json_path"] = str(urls_json_path)
                dump_json(result_path, meta)
                return {
                    "ok": True,
                    "export_only": True,
                    "export_paths": [],
                    "capture_paths": [],
                    "urls_json_path": str(urls_json_path),
                    "outputs_count": 0,
                }

            urls_entries, export_paths, capture_paths = export_outputs_for_labels(
                page, context, all_labels, exports_dir, captures_dir
            )
            dump_json(urls_json_path, urls_entries)
            meta["finished_ms"] = now_ms()
            meta["export_paths"] = export_paths
            meta["capture_paths"] = capture_paths
            meta["urls_json_path"] = str(urls_json_path)
            dump_json(result_path, meta)

            return {
                "ok": True,
                "export_only": True,
                "export_paths": export_paths,
                "capture_paths": capture_paths,
                "urls_json_path": str(urls_json_path),
                "outputs_count": len(all_labels),
            }

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
        description="Variant.com automation: VARIATIONS mode (submit prompt, 4 outputs, export HTML/URL/screenshot).",
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

    export_only = sub.add_parser(
        "export-only",
        help="Reload project at URL and export all existing outputs (links, screenshots, HTML) without generating.",
    )
    export_only.add_argument("--url", required=True, help="Project URL (variant.com/chat/...).")
    export_only.add_argument("--out", required=True, help="Output directory (generators/variant).")
    export_only.add_argument("--headed", action="store_true", help="Run with visible browser.")
    export_only.add_argument("--profile-dir", default=None, help="Chrome profile for persistent login.")
    export_only.add_argument("--connect", default=None, metavar="URL", help="Attach to Chrome via CDP.")

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
    elif ns.cmd == "export-only":
        out_dir = Path(ns.out).resolve()
        profile_dir = Path(ns.profile_dir).resolve() if ns.profile_dir else None
        connect_url = (ns.connect or "").strip() or None
        eargs = ExportOnlyArgs(
            url=ns.url,
            out_dir=out_dir,
            headed=bool(ns.headed),
            profile_dir=profile_dir,
            connect_url=connect_url,
        )
        result = run_variant_export_only(eargs)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        return


if __name__ == "__main__":
    main()
