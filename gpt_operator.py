#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Dict, Any

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeoutError


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

CODE_FENCE_RE = re.compile(r"```(?P<lang>[a-zA-Z0-9_\-+]*)\n(?P<body>.*?)\n```", re.DOTALL)

# Keys we extract from the final raw output (from JSON or from named code blocks).
PROMPT_BLOCKS = ("design_dna_for_aura", "variant_prompt", "aura_edit_instructions")

def read_text_file(p: Path) -> str:
    return p.read_text(encoding="utf-8")

def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def now_ms() -> int:
    return int(time.time() * 1000)

def dump_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def extract_code_blocks(text: str) -> List[Dict[str, str]]:
    blocks = []
    for m in CODE_FENCE_RE.finditer(text or ""):
        blocks.append({
            "lang": (m.group("lang") or "").strip(),
            "content": m.group("body")
        })
    return blocks

def parse_raw_to_json(text: str) -> Optional[Dict[str, Any]]:
    """If the raw output is or contains JSON, return the parsed object; else None."""
    if not (text or "").strip():
        return None
    s = text.strip()
    # 1) Try parsing the whole string as JSON.
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # 2) Try a ```json ... ``` code block.
    for m in CODE_FENCE_RE.finditer(text or ""):
        lang = (m.group("lang") or "").strip().lower()
        if lang == "json":
            try:
                return json.loads(m.group("body"))
            except json.JSONDecodeError:
                pass
    return None

def extract_prompt_blocks(data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract design_dna_for_aura, variant_prompt, aura_edit_instructions from parsed JSON (whichever is available)."""
    out: Dict[str, Any] = {}
    for key in PROMPT_BLOCKS:
        val = None
        if isinstance(data.get("outputs"), dict) and key in data["outputs"]:
            val = data["outputs"][key]
        elif key in data:
            val = data[key]
        if val is not None:
            out[key] = val
    return out

def extract_prompt_blocks_from_code_blocks(blocks: List[Dict[str, str]]) -> Dict[str, Any]:
    """Extract PROMPT_BLOCKS from code blocks whose lang matches the key name."""
    out: Dict[str, Any] = {}
    for b in blocks:
        lang = (b.get("lang") or "").strip()
        if lang in PROMPT_BLOCKS and b.get("content") is not None:
            out[lang] = b["content"]
    return out


# ----------------------------
# Selectors & heuristics
# ----------------------------
# ChatGPT DOM changes sometimes; these are resilient-ish heuristics.
# We prefer role/aria based selectors where possible.

LOGIN_TEXTS = ["Log in", "Sign in", "Sign up", "Create account"]
STOP_TEXTS = ["Stop generating", "Stop"]
COPY_TEXTS = ["Copy", "Copy code"]
SEND_TEXTS = ["Send", "Submit"]

def page_has_auth_gate(page: Page) -> bool:
    body = page.locator("body")
    # Look for login/signup buttons/links.
    for t in LOGIN_TEXTS:
        if body.get_by_role("link", name=re.compile(t, re.I)).count() > 0:
            return True
        if body.get_by_role("button", name=re.compile(t, re.I)).count() > 0:
            return True
    return False

def find_prompt_textarea(page: Page):
    # ChatGPT typically uses a textarea in the composer.
    # Try a few common patterns:
    candidates = [
        page.locator("textarea"),
        page.get_by_role("textbox"),
        page.locator("[contenteditable='true']")
    ]
    for c in candidates:
        if c.count() > 0:
            # choose the first visible one
            for i in range(min(c.count(), 6)):
                el = c.nth(i)
                try:
                    if el.is_visible():
                        return el
                except Exception:
                    pass
    return None

def find_file_input(page: Page):
    # File input used for attachments.
    # We try common accept patterns; if none, any file input.
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
                    # file inputs are sometimes hidden; set_input_files works even if hidden
                    return el
            return c.first
    return None

def click_send(page: Page) -> bool:
    body = page.locator("body")

    # Try explicit "Send"/"Submit" first
    for t in SEND_TEXTS:
        btn = body.get_by_role("button", name=re.compile(t, re.I))
        if btn.count() > 0:
            try:
                btn.first.click(timeout=2000)
                return True
            except Exception:
                pass

    # Try button with aria-label for send
    sendish = page.locator("button[aria-label*='Send'], button[aria-label*='send']")
    if sendish.count() > 0:
        try:
            sendish.first.click(timeout=2000)
            return True
        except Exception:
            pass

    # Last resort: press Enter (composer usually supports Enter-to-send unless multiline)
    ta = find_prompt_textarea(page)
    if ta is not None:
        try:
            ta.press("Enter")
            return True
        except Exception:
            pass

    return False

def stop_button_visible(page: Page) -> bool:
    body = page.locator("body")
    for t in STOP_TEXTS:
        btn = body.get_by_role("button", name=re.compile(t, re.I))
        if btn.count() > 0:
            try:
                return btn.first.is_visible()
            except Exception:
                return True
    # also check aria-label variations
    aria = page.locator("button[aria-label*='Stop'], button[aria-label*='stop']")
    if aria.count() > 0:
        try:
            return aria.first.is_visible()
        except Exception:
            return True
    return False

def wait_until_done(page: Page, timeout_s: int = 120) -> Dict[str, Any]:
    """
    Poll until Stop button disappears (primary signal).
    Returns telemetry about how it ended.
    """
    t0 = time.time()
    seen_stop = False

    while True:
        elapsed = time.time() - t0
        if elapsed > timeout_s:
            return {"done": False, "reason": "timeout", "elapsed_s": round(elapsed, 2), "seen_stop": seen_stop}

        sb = stop_button_visible(page)
        if sb:
            seen_stop = True
        # If we saw stop and it's now gone => done
        if seen_stop and not sb:
            return {"done": True, "reason": "stop_disappeared", "elapsed_s": round(elapsed, 2), "seen_stop": seen_stop}

        time.sleep(0.75)

def click_copy_last_assistant(page: Page) -> Optional[str]:
    """
    Best-effort: find the last visible "Copy" button near the latest assistant message,
    click it, then read clipboard via browser context (where possible).
    """
    body = page.locator("body")

    # Grab all copy-like buttons; pick the last visible
    buttons = []
    for t in COPY_TEXTS:
        btns = body.get_by_role("button", name=re.compile(t, re.I))
        if btns.count() > 0:
            for i in range(btns.count()):
                buttons.append(btns.nth(i))

    # Also some UIs use aria-label
    aria_btns = page.locator("button[aria-label*='Copy'], button[aria-label*='copy']")
    for i in range(min(aria_btns.count(), 50)):
        buttons.append(aria_btns.nth(i))

    # Filter visible
    visible = []
    for b in buttons[-50:]:
        try:
            if b.is_visible():
                visible.append(b)
        except Exception:
            pass

    if not visible:
        return None

    try:
        visible[-1].click(timeout=2000)
    except Exception:
        return None

    # Read clipboard if permissions allow; otherwise return None.
    try:
        # Some Chromium contexts may deny clipboard read; this can fail.
        txt = page.evaluate("() => navigator.clipboard.readText()")
        if isinstance(txt, str) and txt.strip():
            return txt
    except Exception:
        pass

    return None


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
    """Args for re-export: open existing chat URL and re-capture last assistant output (no new prompt)."""
    url: str
    out_dir: Path
    headed: bool
    profile_dir: Optional[Path]
    connect_url: Optional[str]
    settle_timeout_s: int  # max wait for page to settle before copy


def run_gpt_operator(args: RunArgs) -> Dict[str, Any]:
    ensure_dir(args.out_dir)
    meta = {
        "url": args.url,
        "images": args.images,
        "out_dir": str(args.out_dir),
        "started_ms": now_ms(),
    }

    raw_path = args.out_dir / "raw.txt"
    result_path = args.out_dir / "result.json"
    blocks_path = args.out_dir / "blocks.json"
    extracted_path = args.out_dir / "extracted.json"
    debug_html = args.out_dir / "debug.html"
    debug_png = args.out_dir / "debug.png"

    with sync_playwright() as p:
        attached = args.connect_url is not None
        if attached:
            # Use 127.0.0.1 instead of localhost to avoid IPv6 (::1) connection refused on some systems
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
                raise RuntimeError(
                    "No browser context found. Make sure Chrome was started with --remote-debugging-port=9222"
                )
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
                    if args.url.rstrip("/") in u or (u.startswith("https://chatgpt.com") and "/g/" in u):
                        page = tab
                        if args.url.rstrip("/") in u:
                            break
                except Exception:
                    pass
            if page is None and pages:
                page = pages[0]
            if page is None:
                raise RuntimeError(
                    "No tabs found in the attached browser. Open a ChatGPT tab (or the gizmo URL) and re-run with --connect."
                )
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
                    # Keep browser open and wait for user to log in
                    login_timeout_s = 300  # 5 minutes
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
                            page.wait_for_timeout(2000)  # let SPA settle
                            break
                    else:
                        debug_html.write_text(page.content(), encoding="utf-8")
                        page.screenshot(path=str(debug_png), full_page=True)
                        raise RuntimeError(
                            "Login timeout. Log in in the browser and re-run, or increase wait time."
                        )
                else:
                    debug_html.write_text(page.content(), encoding="utf-8")
                    page.screenshot(path=str(debug_png), full_page=True)
                    if attached:
                        raise RuntimeError(
                            "Auth required in the attached tab (Log in/Sign up detected). "
                            "Log in in that browser tab and re-run with --connect."
                        )
                    raise RuntimeError(
                        "Auth required (Log in/Sign up detected). "
                        "Run with --profile-dir and --headed, or use --connect with an already-logged-in Chrome."
                    )

            # Find composer and fill prompt
            composer = find_prompt_textarea(page)
            if composer is None:
                debug_html.write_text(page.content(), encoding="utf-8")
                page.screenshot(path=str(debug_png), full_page=True)
                raise RuntimeError("Could not find prompt input (textarea/textbox/contenteditable).")

            # Clear + type
            try:
                composer.click(timeout=3000)
            except Exception:
                pass
            try:
                composer.fill("")  # textarea supports fill
            except Exception:
                # contenteditable: select all + delete
                try:
                    composer.press("Control+A")
                    composer.press("Backspace")
                except Exception:
                    pass

            # For long prompts, typing is safer than fill in some UIs.
            composer.type(args.prompt, delay=1)

            # Attach images if provided
            if args.images:
                file_input = find_file_input(page)
                if file_input is None:
                    # Not fatal—some UIs require clicking a paperclip first; you can add that later.
                    meta["attach_warning"] = "No file input found; images not attached."
                else:
                    # Playwright accepts list of file paths
                    file_input.set_input_files([str(Path(x).resolve()) for x in args.images])
                    page.wait_for_timeout(800)

            # Submit
            if not click_send(page):
                debug_html.write_text(page.content(), encoding="utf-8")
                page.screenshot(path=str(debug_png), full_page=True)
                raise RuntimeError("Could not submit prompt (send button / enter failed).")

            # Wait until generation completes
            done_info = wait_until_done(page, timeout_s=args.timeout_s)
            meta["done_info"] = done_info

            # Try copy
            copied = click_copy_last_assistant(page)
            meta["copied_via_clipboard"] = bool(copied)

            # Fallback: attempt to read the last assistant message text from DOM (best-effort)
            if not copied:
                # Heuristic: last element with role="article" or common message container.
                candidates = [
                    page.locator("[data-message-author-role='assistant']").last,
                    page.locator("article").last,
                    page.locator("[role='article']").last,
                ]
                text_val = None
                for c in candidates:
                    try:
                        if c.count() > 0:
                            txt = c.inner_text(timeout=2000)
                            if txt and txt.strip():
                                text_val = txt
                                break
                    except Exception:
                        pass
                copied = text_val

            if not copied:
                debug_html.write_text(page.content(), encoding="utf-8")
                page.screenshot(path=str(debug_png), full_page=True)
                raise RuntimeError("Completed, but failed to copy or read assistant output.")

            raw_path.write_text(copied, encoding="utf-8")

            blocks = extract_code_blocks(copied)
            dump_json(blocks_path, blocks)

            # If raw is (or contains) JSON, parse and extract prompt blocks.
            extracted: Dict[str, Any] = {}
            parsed = parse_raw_to_json(copied)
            if parsed is not None:
                extracted = extract_prompt_blocks(parsed)
            # Also pick up any of those keys from named code blocks (e.g. ```design_dna_for_aura ... ```).
            from_blocks = extract_prompt_blocks_from_code_blocks(blocks)
            for k, v in from_blocks.items():
                if v is not None:
                    extracted[k] = v
            dump_json(extracted_path, extracted)

            # Capture current chat/conversation URL for designrun-manager to persist
            try:
                chat_url = (page.url or "").strip() or None
            except Exception:
                chat_url = None
            meta["chat_url"] = chat_url

            meta["finished_ms"] = now_ms()
            meta["raw_path"] = str(raw_path)
            meta["blocks_path"] = str(blocks_path)
            meta["extracted_path"] = str(extracted_path)
            dump_json(result_path, meta)

            return {
                "ok": True,
                "raw_path": str(raw_path),
                "blocks_path": str(blocks_path),
                "extracted_path": str(extracted_path),
                "meta_path": str(result_path),
                "blocks_count": len(blocks),
                "extracted_keys": list(extracted.keys()),
                "done_info": done_info,
                "chat_url": chat_url,
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


def run_gpt_reexport(args: ReexportArgs) -> Dict[str, Any]:
    """
    Re-export only: navigate to an existing ChatGPT conversation URL (e.g. from designrun.json),
    then re-capture the last assistant message to raw.txt, blocks.json, extracted.json. No prompt submit.
    """
    ensure_dir(args.out_dir)
    meta: Dict[str, Any] = {
        "mode": "REEXPORT",
        "url": args.url,
        "out_dir": str(args.out_dir),
        "started_ms": now_ms(),
    }

    raw_path = args.out_dir / "raw.txt"
    result_path = args.out_dir / "result.json"
    blocks_path = args.out_dir / "blocks.json"
    extracted_path = args.out_dir / "extracted.json"
    debug_html = args.out_dir / "debug.html"
    debug_png = args.out_dir / "debug.png"

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
                raise RuntimeError(
                    "No browser context found. Make sure Chrome was started with --remote-debugging-port=9222"
                )
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
                    if args.url.rstrip("/") in u or (u.startswith("https://chatgpt.com") and "/g/" in u):
                        page = tab
                        if args.url.rstrip("/") in u:
                            break
                except Exception:
                    pass
            if page is None and pages:
                page = pages[0]
            if page is None:
                raise RuntimeError(
                    "No tabs found in the attached browser. Open a ChatGPT tab (or the chat URL) and re-run with --connect."
                )
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
                        "Auth required in the attached tab (Log in/Sign up detected). "
                        "Log in in that browser tab and re-run with --connect."
                    )
                raise RuntimeError(
                    "Auth required (Log in/Sign up detected). "
                    "Run with --profile-dir and --headed, or use --connect with an already-logged-in Chrome."
                )

            settle_ms = (min(args.settle_timeout_s, 10) * 1000) if args.settle_timeout_s > 0 else 3000
            page.wait_for_timeout(settle_ms)

            copied = click_copy_last_assistant(page)
            meta["copied_via_clipboard"] = bool(copied)

            if not copied:
                candidates = [
                    page.locator("[data-message-author-role='assistant']").last,
                    page.locator("article").last,
                    page.locator("[role='article']").last,
                ]
                for c in candidates:
                    try:
                        if c.count() > 0:
                            txt = c.inner_text(timeout=2000)
                            if txt and txt.strip():
                                copied = txt
                                break
                    except Exception:
                        pass

            if not copied:
                save_debug(page)
                raise RuntimeError("Re-export: could not copy or read assistant output from the page.")

            raw_path.write_text(copied, encoding="utf-8")

            blocks = extract_code_blocks(copied)
            dump_json(blocks_path, blocks)

            extracted: Dict[str, Any] = {}
            parsed = parse_raw_to_json(copied)
            if parsed is not None:
                extracted = extract_prompt_blocks(parsed)
            from_blocks = extract_prompt_blocks_from_code_blocks(blocks)
            for k, v in from_blocks.items():
                if v is not None:
                    extracted[k] = v
            dump_json(extracted_path, extracted)

            try:
                chat_url = (page.url or "").strip() or None
            except Exception:
                chat_url = None
            meta["chat_url"] = chat_url

            meta["finished_ms"] = now_ms()
            meta["raw_path"] = str(raw_path)
            meta["blocks_path"] = str(blocks_path)
            meta["extracted_path"] = str(extracted_path)
            dump_json(result_path, meta)

            return {
                "ok": True,
                "raw_path": str(raw_path),
                "blocks_path": str(blocks_path),
                "extracted_path": str(extracted_path),
                "meta_path": str(result_path),
                "blocks_count": len(blocks),
                "extracted_keys": list(extracted.keys()),
                "chat_url": chat_url,
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

def load_chat_url_from_designrun(designrun_path: Path) -> str:
    """Read chat_url from designrun.json. Raises if missing or invalid."""
    data = json.loads(designrun_path.read_text(encoding="utf-8"))
    url = (data.get("chat_url") or "").strip()
    if not url:
        raise ValueError(
            f"No 'chat_url' in {designrun_path}. "
            "Run a GPT step first so the conversation URL is saved, or use --url explicitly."
        )
    return url


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gpt_operator", description="Local runner for ChatGPT gizmo automation.")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run a single prompt against a ChatGPT gizmo URL.")
    run.add_argument("--url", required=True, help="ChatGPT gizmo URL (https://chatgpt.com/g/...).")
    run.add_argument("--prompt", default=None, help="Prompt string (use --prompt-file for large prompts).")
    run.add_argument("--prompt-file", default=None, help="Path to a text file containing the prompt.")
    run.add_argument("--image", action="append", default=[], help="Image path to attach (repeatable).")
    run.add_argument("--out", required=True, help="Output directory for artifacts.")
    run.add_argument("--timeout-s", type=int, default=180, help="Timeout waiting for completion.")
    run.add_argument("--headed", action="store_true", help="Run with visible browser window.")
    run.add_argument("--profile-dir", default=None, help="Chrome profile dir for persistent login session.")
    run.add_argument(
        "--connect",
        default=None,
        metavar="URL",
        help="Attach to existing Chrome via CDP (e.g. http://localhost:9222). Use the tab you have open and logged in.",
    )

    reexport = sub.add_parser(
        "re-export",
        help="Re-export only: open existing chat URL and re-capture last assistant output (no new prompt).",
    )
    reexport.add_argument(
        "--designrun",
        metavar="PATH",
        help="Path to designrun.json; chat_url is read from here.",
    )
    reexport.add_argument(
        "--url",
        help="ChatGPT conversation URL (e.g. https://chatgpt.com/g/.../c/...). Use if not using --designrun.",
    )
    reexport.add_argument("--out", required=True, help="Output directory (e.g. steps/<step>/gpt).")
    reexport.add_argument(
        "--settle-timeout-s",
        type=int,
        default=3,
        help="Seconds to wait for page to settle before copying (default 3).",
    )
    reexport.add_argument("--headed", action="store_true", help="Run with visible browser window.")
    reexport.add_argument("--profile-dir", default=None, help="Chrome profile dir for persistent login session.")
    reexport.add_argument(
        "--connect",
        default=None,
        metavar="URL",
        help="Attach to existing Chrome via CDP.",
    )
    return p

def main():
    parser = build_parser()
    ns = parser.parse_args()

    if ns.cmd == "run":
        if (ns.prompt is None) == (ns.prompt_file is None):
            print("Provide exactly one of --prompt or --prompt-file", file=sys.stderr)
            sys.exit(2)

        prompt = ns.prompt if ns.prompt is not None else read_text_file(Path(ns.prompt_file))
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

        result = run_gpt_operator(rargs)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    if ns.cmd == "re-export":
        out_dir = Path(ns.out).resolve()
        profile_dir = Path(ns.profile_dir).resolve() if ns.profile_dir else None
        connect_url = (ns.connect or "").strip() or None
        if getattr(ns, "designrun", None):
            url = load_chat_url_from_designrun(Path(ns.designrun).resolve())
        elif (getattr(ns, "url", None) or "").strip():
            url = (ns.url or "").strip()
        else:
            parser.error("re-export: provide --designrun PATH or --url URL.")
        rargs = ReexportArgs(
            url=url,
            out_dir=out_dir,
            headed=bool(ns.headed),
            profile_dir=profile_dir,
            connect_url=connect_url,
            settle_timeout_s=int(getattr(ns, "settle_timeout_s", 3)),
        )
        result = run_gpt_reexport(rargs)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    parser.error(f"Unknown command: {ns.cmd}")


if __name__ == "__main__":
    main()
