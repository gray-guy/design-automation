"""
Scroll-by-viewport full-page screenshot: scroll one viewport at a time, capture each view,
stitch into one long image. Used by aura_operator and variant_operator.
Handles iframes, non-integer DPR, fixed/sticky elements, scroll animations,
and both window-scrolling pages and inner scroll containers.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

if TYPE_CHECKING:
    from playwright.sync_api import Page


# ---------------------------------------------------------------------------
# JavaScript helpers
# ---------------------------------------------------------------------------

# Pick the scroll root with the largest scrollable range (window vs best inner element).
# Mark it with data-pw-scroll-root so other JS reads from it.
_FIND_AND_MARK_SCROLL_JS = """
() => {
  const vh = window.innerHeight;
  const docH = Math.max(
    document.body.scrollHeight,
    document.documentElement.scrollHeight,
    document.body.offsetHeight || 0,
    document.documentElement.offsetHeight || 0
  );
  const windowMax = Math.max(0, docH - vh);
  let bestEl = null;
  let bestMax = windowMax;

  document.querySelectorAll('[data-pw-scroll-root]').forEach(el => el.removeAttribute('data-pw-scroll-root'));
  const candidates = Array.from(document.querySelectorAll('*'));
  for (const el of candidates) {
    const style = window.getComputedStyle(el);
    const oy = style.overflowY || style.overflow;
    if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight) {
      const elMax = el.scrollHeight - el.clientHeight;
      if (elMax > bestMax) {
        bestMax = elMax;
        bestEl = el;
      }
    }
  }
  if (bestEl) {
    bestEl.setAttribute('data-pw-scroll-root', '1');
  }
  return { max: bestMax, useWindow: !bestEl };
}
"""

_SCROLL_TO_JS = """
(y) => {
  const el = document.querySelector('[data-pw-scroll-root="1"]');
  if (el) el.scrollTop = y;
  else window.scrollTo(0, y);
}
"""

# Single source of truth: current scroll position and maximum scrollable distance.
_GET_SCROLL_STATE_JS = """
() => {
  const el = document.querySelector('[data-pw-scroll-root="1"]');
  if (el) {
    const max = Math.max(0, el.scrollHeight - el.clientHeight);
    return { position: el.scrollTop, max };
  }
  const docH = Math.max(
    document.body.scrollHeight,
    document.documentElement.scrollHeight,
    document.body.offsetHeight || 0,
    document.documentElement.offsetHeight || 0
  );
  const max = Math.max(0, docH - window.innerHeight);
  return { position: window.scrollY || window.pageYOffset || 0, max };
}
"""

# Return scrollTop for window and each scrollable element (stable order) for observation.
_GET_SCROLLABLE_STATES_JS = """
() => {
  const result = [{ type: 'window', scrollTop: window.scrollY || window.pageYOffset || 0 }];
  const scrollable = Array.from(document.querySelectorAll('*')).filter(el => {
    const style = window.getComputedStyle(el);
    const oy = style.overflowY || style.overflow;
    return (oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight;
  });
  scrollable.forEach((el, i) => result.push({ type: 'element', index: i, scrollTop: el.scrollTop }));
  return result;
}
"""

# Mark the scroll root discovered by observation.
_MARK_SCROLL_ROOT_BY_OBSERVATION_JS = """
(arg) => {
  document.querySelectorAll('[data-pw-scroll-root]').forEach(el => el.removeAttribute('data-pw-scroll-root'));
  if (arg.type === 'window') return;
  const scrollable = Array.from(document.querySelectorAll('*')).filter(el => {
    const style = window.getComputedStyle(el);
    const oy = style.overflowY || style.overflow;
    return (oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight;
  });
  const el = scrollable[arg.index];
  if (el) el.setAttribute('data-pw-scroll-root', '1');
}
"""

# Freeze CSS animations/transitions and force backgrounds to scroll with content.
_DISABLE_ANIMATIONS_JS = """
() => {
  const style = document.createElement('style');
  style.id = '__pw_no_anim';
  style.textContent = [
    '*, *::before, *::after { transition: none !important; animation: none !important; }',
    '*, *::before, *::after { background-attachment: scroll !important; }'
  ].join('\\n');
  document.head.appendChild(style);
}
"""

_HIDE_FIXED_JS = """
() => {
  for (const el of document.querySelectorAll('*')) {
    const style = window.getComputedStyle(el);
    if (style.position === 'fixed' || style.position === 'sticky') {
      el.style.visibility = 'hidden';
    }
  }
}
"""

_SHOW_FIXED_JS = """
() => {
  for (const el of document.querySelectorAll('*')) {
    if (el.style.visibility === 'hidden') {
      el.style.visibility = '';
    }
  }
}
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _scroll_to_target(
    page: Any,
    target_y: int,
    get_state: Any,
    center_x: int,
    center_y: int,
    wheel_chunk: int,
    wheel_wait_ms: int,
    settle_ms: int,
    max_attempts: int = 150,
    eval_context: Any = None,
) -> None:
    """Scroll to *target_y*: try JS first, then wheel up or down until we reach it."""
    ctx = eval_context or page
    ctx.evaluate(_SCROLL_TO_JS, target_y)
    page.wait_for_timeout(settle_ms)
    pos, _ = get_state()
    if pos == target_y:
        return
    if pos > target_y:
        for _ in range(max_attempts):
            page.mouse.move(center_x, center_y)
            page.mouse.wheel(0, -wheel_chunk)
            page.wait_for_timeout(wheel_wait_ms)
            pos, _ = get_state()
            if pos <= target_y:
                break
    else:
        last_pos = pos
        no_advance = 0
        for _ in range(max_attempts):
            page.mouse.move(center_x, center_y)
            page.mouse.wheel(0, wheel_chunk)
            page.wait_for_timeout(wheel_wait_ms)
            pos, _ = get_state()
            if pos >= target_y:
                break
            if pos > last_pos:
                last_pos = pos
                no_advance = 0
            else:
                no_advance += 1
                if no_advance >= 15:
                    break
    page.wait_for_timeout(settle_ms)


def _capture_full_page_wheel(
    page: Any,
    path: Path,
    settle_ms: int = 200,
    wheel_chunk: int = 200,
    max_tiles: int = 80,
    wheel_wait_ms: int = 80,
) -> Path:
    """
    Full-height screenshot: scroll viewport-by-viewport with mouse wheel,
    capture tiles, stitch with overlap-aware contiguous placement.
    Handles iframes, non-integer DPR, fixed elements, and scroll animations.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    dims = page.evaluate("() => ({ w: window.innerWidth, h: window.innerHeight })")
    vw = int(dims.get("w", 1280))
    vh = int(dims.get("h", 720))
    if vh <= 0:
        vh = 720
    if vw <= 0:
        vw = 1280

    center_x, center_y = vw // 2, vh // 2

    # --- Detect iframe: if page has an iframe with large content, use its frame for scroll JS ---
    eval_context: Any = page  # default: evaluate JS in main frame
    iframe_detected = False
    try:
        iframe_info = page.evaluate("""
        () => {
          const iframes = document.querySelectorAll('iframe');
          for (let i = 0; i < iframes.length; i++) {
            try {
              const fd = iframes[i].contentDocument;
              if (fd && fd.documentElement.scrollHeight > window.innerHeight) {
                return { index: i, docH: fd.documentElement.scrollHeight };
              }
            } catch(e) {}
          }
          return null;
        }
        """)
        if isinstance(iframe_info, dict):
            iframe_el = page.query_selector(f"iframe:nth-of-type({iframe_info['index'] + 1})")
            if iframe_el:
                content_frame = iframe_el.content_frame()
                if content_frame:
                    eval_context = content_frame
                    iframe_detected = True
    except Exception:
        pass

    eval_context.evaluate(_FIND_AND_MARK_SCROLL_JS)

    eval_context.evaluate(_SCROLL_TO_JS, 0)
    page.wait_for_timeout(settle_ms)

    # Discover scroll root by observation: which element's scrollTop increases when we wheel
    before_states = eval_context.evaluate(_GET_SCROLLABLE_STATES_JS)
    for _ in range(8):
        page.mouse.move(center_x, center_y)
        page.mouse.wheel(0, wheel_chunk)
        page.wait_for_timeout(wheel_wait_ms)
    page.wait_for_timeout(settle_ms)
    after_states = eval_context.evaluate(_GET_SCROLLABLE_STATES_JS)

    best_delta = 0
    best_entry: Any = None
    if isinstance(before_states, list) and isinstance(after_states, list) and len(before_states) == len(after_states):
        for b, a in zip(before_states, after_states):
            if not isinstance(b, dict) or not isinstance(a, dict):
                continue
            st_b = int(b.get("scrollTop", 0))
            st_a = int(a.get("scrollTop", 0))
            d = st_a - st_b
            if st_a > st_b and d > best_delta:
                best_delta = d
                best_entry = {"type": b.get("type", "window"), "index": b.get("index", 0)}

    # Always wheel back up after observation to undo visual scroll
    for _ in range(8):
        page.mouse.move(center_x, center_y)
        page.mouse.wheel(0, -wheel_chunk)
        page.wait_for_timeout(wheel_wait_ms)
    page.wait_for_timeout(settle_ms)

    if best_entry:
        eval_context.evaluate(_MARK_SCROLL_ROOT_BY_OBSERVATION_JS, best_entry)
    else:
        eval_context.evaluate(
            "() => document.querySelectorAll('[data-pw-scroll-root]').forEach(el => el.removeAttribute('data-pw-scroll-root'))"
        )

    def get_state() -> tuple[int, int]:
        s = eval_context.evaluate(_GET_SCROLL_STATE_JS)
        if not s or not isinstance(s, dict):
            return (0, vh)
        pos = int(s.get("position", 0))
        max_pos = int(s.get("max", vh))
        return (pos, max_pos)

    # Trigger all scroll-driven animations by scrolling to the bottom, then freeze
    _, max_scroll = get_state()
    if max_scroll > 0:
        _scroll_to_target(
            page, max_scroll, get_state, center_x, center_y,
            wheel_chunk, wheel_wait_ms, settle_ms, eval_context=eval_context,
        )
        page.wait_for_timeout(500)  # let animations finish

    # Freeze animations/transitions and fix viewport-relative backgrounds so tiles stitch cleanly
    eval_context.evaluate(_DISABLE_ANIMATIONS_JS)

    # Hide outer-page overlays (e.g. Variant badge) so they don't repeat in every tile
    if iframe_detected:
        try:
            page.evaluate("""
            () => {
              const iframe = document.querySelector('iframe');
              for (const el of document.querySelectorAll('*')) {
                if (el === iframe || el.contains(iframe) || iframe.contains(el)) continue;
                if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE' || el.tagName === 'LINK' || el.tagName === 'HEAD') continue;
                const style = window.getComputedStyle(el);
                if (style.position === 'fixed' || style.position === 'absolute' || style.position === 'sticky') {
                  el.setAttribute('data-pw-hidden-overlay', el.style.visibility || '');
                  el.style.visibility = 'hidden';
                }
              }
            }
            """)
        except Exception:
            pass

    # Scroll back to top
    _scroll_to_target(
        page, 0, get_state, center_x, center_y,
        wheel_chunk, wheel_wait_ms, settle_ms, eval_context=eval_context,
    )
    pos, _ = get_state()
    if pos != 0:
        for _ in range(50):
            page.mouse.move(center_x, center_y)
            page.mouse.wheel(0, -wheel_chunk)
            page.wait_for_timeout(wheel_wait_ms)
            pos, _ = get_state()
            if pos <= 0:
                break
        page.wait_for_timeout(settle_ms)

    # ---- Tile capture loop ----
    tiles: list[bytes] = []
    tile_positions: list[int] = []
    while len(tiles) < max_tiles:
        step_start, _ = get_state()
        tiles.append(page.screenshot())
        tile_positions.append(step_start)

        # After first tile, hide fixed/sticky elements so they don't repeat
        if len(tiles) == 1:
            try:
                eval_context.evaluate(_HIDE_FIXED_JS)
            except Exception:
                pass

        # Scroll less than a full viewport so consecutive tiles overlap
        overlap_margin = max(100, vh // 8)
        target_pos = step_start + vh - overlap_margin
        last_pos = step_start
        no_advance = 0
        for _ in range(100):
            page.mouse.move(center_x, center_y)
            page.mouse.wheel(0, wheel_chunk)
            page.wait_for_timeout(wheel_wait_ms)
            curr_pos, _ = get_state()
            if curr_pos >= target_pos:
                break
            if curr_pos > last_pos:
                last_pos = curr_pos
                no_advance = 0
            else:
                no_advance += 1
                if no_advance >= 15:
                    break
        # Fine-tune: use JS scroll to land exactly at target_pos
        eval_context.evaluate(_SCROLL_TO_JS, target_pos)
        page.wait_for_timeout(settle_ms)
        end_pos, _ = get_state()
        if end_pos <= step_start:
            break

    # Restore fixed/sticky elements, re-enable animations, restore outer page overlays
    try:
        eval_context.evaluate(_SHOW_FIXED_JS)
        eval_context.evaluate("() => { const s = document.getElementById('__pw_no_anim'); if (s) s.remove(); }")
    except Exception:
        pass
    if iframe_detected:
        try:
            page.evaluate("""
            () => {
              for (const el of document.querySelectorAll('[data-pw-hidden-overlay]')) {
                el.style.visibility = el.getAttribute('data-pw-hidden-overlay') || '';
                el.removeAttribute('data-pw-hidden-overlay');
              }
            }
            """)
        except Exception:
            pass

    if not tiles:
        page.screenshot(path=str(path))
        try:
            eval_context.evaluate(
                "() => document.querySelector('[data-pw-scroll-root]')?.removeAttribute('data-pw-scroll-root')"
            )
        except Exception:
            pass
        return path

    # ---- Contiguous overlap-aware stitching with exact scale factor ----
    # Screenshots may be larger than CSS pixels (e.g. 1.1x on 110% Windows scaling).
    # Paste positions are computed sequentially so tiles are always contiguous (no rounding gaps).
    n = len(tiles)
    last_pos_captured = tile_positions[-1]
    content_height_css = last_pos_captured + vh
    images = [Image.open(io.BytesIO(t)) for t in tiles]
    img_h = images[0].height
    img_w = images[0].width
    scale = img_h / vh if vh > 0 else 1.0

    stitch_h = int(round(content_height_css * scale))
    stitch_w = img_w
    stitched = Image.new(images[0].mode, (stitch_w, stitch_h))

    next_paste_y = 0
    for i in range(n):
        y_css = tile_positions[i]
        img = images[i]
        crop_top_px = 0
        if i > 0:
            prev_end_css = tile_positions[i - 1] + vh
            overlap_css = max(0, prev_end_css - tile_positions[i])
            crop_top_px = int(round(overlap_css * scale))
        crop_bottom_px = min(img.height, int(round(min(vh, content_height_css - y_css) * scale)))
        if crop_top_px >= crop_bottom_px:
            continue
        cropped = img.crop((0, crop_top_px, img_w, crop_bottom_px))
        paste_y = next_paste_y if i > 0 else 0
        if paste_y + cropped.height > stitch_h:
            cropped = cropped.crop((0, 0, img_w, stitch_h - paste_y))
        if cropped.height > 0:
            stitched.paste(cropped, (0, paste_y))
        next_paste_y = paste_y + cropped.height

    stitched.save(str(path), "PNG")

    try:
        eval_context.evaluate(
            "() => document.querySelector('[data-pw-scroll-root]')?.removeAttribute('data-pw-scroll-root')"
        )
    except Exception:
        pass

    return path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def capture_full_page_scrolled(
    page: "Page",
    path: Path,
    settle_ms: int = 200,
) -> Path:
    """
    Full-height screenshot by scrolling one viewport at a time and stitching.
    Uses mouse wheel so pages that ignore programmatic JS scroll actually move.

    Handles iframes, non-integer DPR, fixed/sticky elements, and scroll animations.
    Returns the path where the image was saved.
    """
    return _capture_full_page_wheel(page, path, settle_ms=settle_ms)
