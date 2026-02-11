#!/usr/bin/env python3
"""
designrun-manager: Main control for UI design automation.
Owns run/step filesystem layout, designrun.json, events.ndjson;
invokes gpt_operator (and later aura/variant) and normalizes outputs.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ----------------------------
# Config & paths
# ----------------------------

DEFAULT_RUNS_DIR = "runs"
MODES = ("DNA", "VARIATIONS", "FEEDBACK")

# Reference image limits (enforced in add_references and when passing images to operators)
MAX_REFERENCE_IMAGES = 2
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB

# GPT extracted keys -> step gpt/outputs/ filename (without path)
EXTRACTED_KEY_TO_FILE = {
    "design_dna_for_aura": "aura_dna.txt",
    "variant_prompt": "variant_prompt.txt",
    "aura_edit_instructions": "aura_edit.txt",
}


def get_runs_dir() -> Path:
    return Path(os.environ.get("DESIGN_RUNS_DIR", DEFAULT_RUNS_DIR)).resolve()


def get_run_dir(run_id: str) -> Path:
    return get_runs_dir() / run_id


def get_steps_dir(run_id: str) -> Path:
    return get_run_dir(run_id) / "steps"


def get_step_dir(run_id: str, step_id: str) -> Path:
    return get_steps_dir(run_id) / step_id


def get_project_root() -> Path:
    """Project root (directory containing designrun_manager.py and config)."""
    return Path(__file__).resolve().parent


def get_gpt_operator_script() -> Path:
    """Path to gpt_operator.py (same directory as this script)."""
    return get_project_root() / "gpt_operator.py"


def get_aura_operator_script() -> Path:
    """Path to aura_operator.py (same directory as this script)."""
    return get_project_root() / "aura_operator.py"


def get_variant_operator_script() -> Path:
    """Path to variant_operator.py (same directory as this script)."""
    return get_project_root() / "variant_operator.py"


def read_config() -> Dict[str, Any]:
    """Read config.json from project root. Missing or invalid file returns {}."""
    path = get_project_root() / "config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ----------------------------
# designrun.json
# ----------------------------


def read_designrun(run_dir: Path) -> Dict[str, Any]:
    p = run_dir / "designrun.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def write_designrun(run_dir: Path, data: Dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    p = run_dir / "designrun.json"
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def update_designrun(run_dir: Path, updates: Dict[str, Any]) -> None:
    data = read_designrun(run_dir)
    data.update(updates)
    write_designrun(run_dir, data)


# ----------------------------
# events.ndjson
# ----------------------------


def now_ts() -> int:
    return int(time.time() * 1000)


def append_event(run_dir: Path, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    event = {"event": event_type, "ts": now_ts()}
    if payload:
        event.update(payload)
    with open(run_dir / "events.ndjson", "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


# ----------------------------
# Run & step layout
# ----------------------------


def ensure_step_layout(step_dir: Path) -> None:
    """Create full step folder structure."""
    (step_dir / "input").mkdir(parents=True, exist_ok=True)
    (step_dir / "references" / "images").mkdir(parents=True, exist_ok=True)
    (step_dir / "gpt" / "outputs").mkdir(parents=True, exist_ok=True)
    (step_dir / "generators" / "aura").mkdir(parents=True, exist_ok=True)
    (step_dir / "generators" / "aura" / "exports").mkdir(parents=True, exist_ok=True)
    (step_dir / "generators" / "aura" / "captures").mkdir(parents=True, exist_ok=True)
    (step_dir / "generators" / "variant").mkdir(parents=True, exist_ok=True)
    (step_dir / "generators" / "variant" / "exports").mkdir(parents=True, exist_ok=True)
    (step_dir / "generators" / "variant" / "captures").mkdir(parents=True, exist_ok=True)


def list_step_ids(run_id: str) -> List[str]:
    steps_dir = get_steps_dir(run_id)
    if not steps_dir.exists():
        return []
    return sorted(d.name for d in steps_dir.iterdir() if d.is_dir() and d.name.startswith("S"))


def next_step_number(run_id: str) -> int:
    existing = list_step_ids(run_id)
    if not existing:
        return 1
    # S01_name -> 1, S02_foo -> 2
    numbers = []
    for s in existing:
        try:
            num = int(s.split("_")[0][1:])
            numbers.append(num)
        except (ValueError, IndexError):
            pass
    return max(numbers, default=0) + 1


def init_run(run_id: str) -> Path:
    """Create run folder, designrun.json, events.ndjson, steps/."""
    run_dir = get_run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "steps").mkdir(exist_ok=True)
    if not (run_dir / "designrun.json").exists():
        write_designrun(run_dir, {
            "run_id": run_id,
            "created_ts": now_ts(),
            "chat_url": None,
            "aura_project_url": None,
            "variant_project_url": None,
        })
    if not (run_dir / "events.ndjson").exists():
        append_event(run_dir, "run_created", {"run_id": run_id})
    return run_dir


def add_step(run_id: str, name: str) -> str:
    """Create step SXX_<name> with full layout. Returns step_id."""
    init_run(run_id)
    num = next_step_number(run_id)
    step_id = f"S{num:02d}_{name}"
    step_dir = get_step_dir(run_id, step_id)
    ensure_step_layout(step_dir)
    run_dir = get_run_dir(run_id)
    append_event(run_dir, "step_created", {"step_id": step_id})
    return step_id


# ----------------------------
# Inputs & references
# ----------------------------


def set_step_input(run_id: str, step_id: str, user_text: str, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    step_dir = get_step_dir(run_id, step_id)
    ensure_step_layout(step_dir)
    (step_dir / "input" / "user_text.txt").write_text(user_text, encoding="utf-8")
    (step_dir / "input" / "mode.txt").write_text(mode, encoding="utf-8")
    append_event(get_run_dir(run_id), "step_input_saved", {"step_id": step_id})


def validate_reference_images(image_paths: List[str]) -> None:
    """Raise ValueError if more than MAX_REFERENCE_IMAGES or any image is >= MAX_IMAGE_SIZE_BYTES."""
    if len(image_paths) > MAX_REFERENCE_IMAGES:
        raise ValueError(
            f"At most {MAX_REFERENCE_IMAGES} reference images allowed, got {len(image_paths)}."
        )
    for src in image_paths:
        src_path = Path(src).resolve()
        if not src_path.exists():
            raise FileNotFoundError(f"Reference image not found: {src_path}")
        size = src_path.stat().st_size
        if size >= MAX_IMAGE_SIZE_BYTES:
            raise ValueError(
                f"Reference image too large: {src_path.name} ({size / (1024*1024):.2f} MB). "
                f"Maximum size is {MAX_IMAGE_SIZE_BYTES // (1024*1024)} MB."
            )


def add_references(
    run_id: str,
    step_id: str,
    image_paths: List[str],
    map_labels: Optional[Dict[str, str]] = None,
) -> None:
    """
    Copy images to step references/images/ as ref_001.<ext>, ref_002.<ext>, ...
    and write references/map.json (filename -> label/meaning).
    At most MAX_REFERENCE_IMAGES images; each must be < MAX_IMAGE_SIZE_BYTES.
    """
    validate_reference_images(image_paths)
    step_dir = get_step_dir(run_id, step_id)
    ensure_step_layout(step_dir)
    ref_dir = step_dir / "references" / "images"
    map_data: Dict[str, str] = {}
    for i, src in enumerate(image_paths, start=1):
        src_path = Path(src).resolve()
        if not src_path.exists():
            raise FileNotFoundError(f"Reference image not found: {src_path}")
        ext = src_path.suffix or ".png"
        dest_name = f"ref_{i:03d}{ext}"
        dest_path = ref_dir / dest_name
        shutil.copy2(src_path, dest_path)
        label = (map_labels or {}).get(src, (map_labels or {}).get(src_path.name, f"Reference {i}"))
        map_data[dest_name] = label
    (step_dir / "references" / "map.json").write_text(
        json.dumps(map_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    append_event(get_run_dir(run_id), "references_saved", {"step_id": step_id, "count": len(image_paths)})


def save_artifact(run_id: str, step_id: str, relative_path: str, content: bytes | str, is_text: bool = True) -> Path:
    """Write an arbitrary artifact under the step (e.g. gpt/foo.json)."""
    step_dir = get_step_dir(run_id, step_id)
    dest = step_dir / relative_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    if is_text and isinstance(content, str):
        dest.write_text(content, encoding="utf-8")
    else:
        dest.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return dest


# ----------------------------
# GPT: invoke + normalize
# ----------------------------


def normalize_gpt_output(gpt_dir: Path) -> None:
    """
    After gpt_operator has written to gpt_dir: build response.json and gpt/outputs/*.txt.
    - response.json = {"raw": "<text>", "blocks_count": N, "extracted_keys": [...]}
    - outputs from extracted.json: design_dna_for_aura -> aura_dna.txt, etc.
    """
    raw_path = gpt_dir / "raw.txt"
    extracted_path = gpt_dir / "extracted.json"
    blocks_path = gpt_dir / "blocks.json"

    if not raw_path.exists():
        return

    raw_text = raw_path.read_text(encoding="utf-8")
    blocks_count = 0
    if blocks_path.exists():
        try:
            blocks = json.loads(blocks_path.read_text(encoding="utf-8"))
            blocks_count = len(blocks) if isinstance(blocks, list) else 0
        except Exception:
            pass

    extracted: Dict[str, Any] = {}
    if extracted_path.exists():
        try:
            extracted = json.loads(extracted_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    response = {
        "raw": raw_text,
        "blocks_count": blocks_count,
        "extracted_keys": list(extracted.keys()),
    }
    (gpt_dir / "response.json").write_text(
        json.dumps(response, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    outputs_dir = gpt_dir / "outputs"
    outputs_dir.mkdir(exist_ok=True)
    for key, filename in EXTRACTED_KEY_TO_FILE.items():
        val = extracted.get(key)
        if val is None:
            continue
        if isinstance(val, (dict, list)):
            text = json.dumps(val, indent=2, ensure_ascii=False)
        else:
            text = str(val)
        (outputs_dir / filename).write_text(text, encoding="utf-8")


def run_gpt(
    run_id: str,
    step_id: str,
    *,
    url: Optional[str] = None,
    timeout_s: int = 180,
    headed: bool = False,
    profile_dir: Optional[str] = None,
    connect: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run gpt_operator for the given step; normalize outputs; update designrun.json chat_url.
    Uses designrun.json chat_url or provided url for the gizmo/conversation URL.
    """
    run_dir = get_run_dir(run_id)
    step_dir = get_step_dir(run_id, step_id)
    designrun = read_designrun(run_dir)

    # URL: CLI --url > designrun.chat_url (resume) > config.json chatgpt_url
    effective_url = url or designrun.get("chat_url") or read_config().get("chatgpt_url")
    if not effective_url:
        raise ValueError(
            "No ChatGPT URL. Set chatgpt_url in config.json, or pass --url, or run once with --url to set chat_url in designrun.json."
        )

    prompt_file = step_dir / "input" / "user_text.txt"
    if not prompt_file.exists():
        raise FileNotFoundError(f"Step input not found: {prompt_file}. Set input first (set-input).")

    gpt_dir = step_dir / "gpt"
    gpt_dir.mkdir(parents=True, exist_ok=True)

    ref_images_dir = step_dir / "references" / "images"
    image_args: List[str] = []
    if ref_images_dir.exists():
        candidates = sorted(ref_images_dir.iterdir())
        for p in candidates:
            if not p.is_file():
                continue
            if p.stat().st_size >= MAX_IMAGE_SIZE_BYTES:
                continue  # skip oversized (e.g. manually added)
            image_args.append(str(p))
            if len(image_args) >= MAX_REFERENCE_IMAGES:
                break

    append_event(run_dir, "gpt_started", {"step_id": step_id})

    script = get_gpt_operator_script()
    cmd = [
        sys.executable,
        str(script),
        "run",
        "--url", effective_url,
        "--prompt-file", str(prompt_file),
        "--out", str(gpt_dir),
        "--timeout-s", str(timeout_s),
    ]
    for img in image_args:
        cmd.extend(["--image", img])
    if headed:
        cmd.append("--headed")
    if profile_dir:
        cmd.append("--profile-dir")
        cmd.append(profile_dir)
    if connect:
        cmd.append("--connect")
        cmd.append(connect)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s + 30,
            cwd=str(script.parent),
        )
    except subprocess.TimeoutExpired as e:
        append_event(run_dir, "gpt_finished", {"step_id": step_id, "success": False, "error": "timeout"})
        raise RuntimeError("gpt_operator timed out") from e

    if result.returncode != 0:
        append_event(run_dir, "gpt_finished", {
            "step_id": step_id,
            "success": False,
            "error": (result.stderr or result.stdout or "non-zero exit")[:500],
        })
        raise RuntimeError(f"gpt_operator failed: {result.stderr or result.stdout}")

    # Normalize: response.json + gpt/outputs/*.txt
    normalize_gpt_output(gpt_dir)

    # Update designrun.json with chat_url from result
    result_path = gpt_dir / "result.json"
    if result_path.exists():
        try:
            meta = json.loads(result_path.read_text(encoding="utf-8"))
            chat_url = meta.get("chat_url")
            if chat_url:
                update_designrun(run_dir, {"chat_url": chat_url})
        except Exception:
            pass

    append_event(run_dir, "gpt_finished", {"step_id": step_id, "success": True})
    return {"ok": True, "step_id": step_id, "gpt_dir": str(gpt_dir)}


# ----------------------------
# Aura: invoke + update designrun
# ----------------------------


def run_aura(
    run_id: str,
    step_id: str,
    *,
    url: Optional[str] = None,
    timeout_s: int = 150,
    headed: bool = False,
    profile_dir: Optional[str] = None,
    connect: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run aura_operator for the given step (DNA or FEEDBACK from step input/mode.txt).
    Update designrun.json aura_project_url when mode is DNA and operator returns it.
    """
    run_dir = get_run_dir(run_id)
    step_dir = get_step_dir(run_id, step_id)
    designrun = read_designrun(run_dir)

    mode_path = step_dir / "input" / "mode.txt"
    if not mode_path.exists():
        raise ValueError(f"Step mode not set: {mode_path}. Set input first (set-input).")
    mode = mode_path.read_text(encoding="utf-8").strip().upper()
    if mode not in ("DNA", "FEEDBACK"):
        raise ValueError(f"Aura supports only DNA or FEEDBACK mode; step mode is {mode!r}.")

    if mode == "DNA":
        prompt_file = step_dir / "gpt" / "outputs" / "aura_dna.txt"
        effective_url = url or read_config().get("aura_start_url") or "https://www.aura.build/"
    else:
        prompt_file = step_dir / "gpt" / "outputs" / "aura_edit.txt"
        effective_url = designrun.get("aura_project_url") or url
        if not effective_url:
            raise ValueError(
                "FEEDBACK mode requires aura_project_url in designrun.json. "
                "Run Aura in DNA mode first, or pass --url with the project URL."
            )

    if not prompt_file.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_file}. Run run-gpt first to generate it.")

    out_dir = step_dir / "generators" / "aura"
    ensure_step_layout(step_dir)

    ref_images_dir = step_dir / "references" / "images"
    image_args: List[str] = []
    if ref_images_dir.exists():
        candidates = sorted(ref_images_dir.iterdir())
        for p in candidates:
            if not p.is_file():
                continue
            if p.stat().st_size >= MAX_IMAGE_SIZE_BYTES:
                continue  # skip oversized (e.g. manually added)
            image_args.append(str(p))
            if len(image_args) >= MAX_REFERENCE_IMAGES:
                break

    append_event(run_dir, "aura_started", {"step_id": step_id, "mode": mode})

    script = get_aura_operator_script()
    cmd = [
        sys.executable,
        str(script),
        "run",
        "--mode", mode,
        "--url", effective_url,
        "--prompt-file", str(prompt_file),
        "--out", str(out_dir),
        "--timeout-s", str(timeout_s),
    ]
    for img in image_args:
        cmd.extend(["--image", img])
    if headed:
        cmd.append("--headed")
    if profile_dir:
        cmd.append("--profile-dir")
        cmd.append(profile_dir)
    if connect:
        cmd.append("--connect")
        cmd.append(connect)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s + 60,
            cwd=str(script.parent),
        )
    except subprocess.TimeoutExpired as e:
        append_event(run_dir, "aura_finished", {"step_id": step_id, "success": False, "error": "timeout"})
        raise RuntimeError("aura_operator timed out") from e

    if result.returncode != 0:
        append_event(run_dir, "aura_finished", {
            "step_id": step_id,
            "success": False,
            "error": (result.stderr or result.stdout or "non-zero exit")[:500],
        })
        raise RuntimeError(f"aura_operator failed: {result.stderr or result.stdout}")

    # Parse JSON from stdout to get aura_project_url (DNA)
    aura_project_url = None
    try:
        out = (result.stdout or "").strip()
        if out:
            parsed = json.loads(out)
            aura_project_url = parsed.get("aura_project_url")
    except Exception:
        pass
    if aura_project_url:
        update_designrun(run_dir, {"aura_project_url": aura_project_url})

    append_event(run_dir, "aura_finished", {"step_id": step_id, "success": True})
    return {"ok": True, "step_id": step_id, "aura_dir": str(out_dir), "aura_project_url": aura_project_url}


# ----------------------------
# Variant: invoke + update designrun
# ----------------------------


def run_variant(
    run_id: str,
    step_id: str,
    *,
    url: Optional[str] = None,
    timeout_s: int = 300,
    headed: bool = False,
    profile_dir: Optional[str] = None,
    connect: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run variant_operator for the given step. Only supports VARIATIONS mode (from input/mode.txt).
    Update designrun.json variant_project_url when the operator returns it (e.g. first run).
    """
    run_dir = get_run_dir(run_id)
    step_dir = get_step_dir(run_id, step_id)
    designrun = read_designrun(run_dir)

    mode_path = step_dir / "input" / "mode.txt"
    if not mode_path.exists():
        raise ValueError(f"Step mode not set: {mode_path}. Set input first (set-input).")
    mode = mode_path.read_text(encoding="utf-8").strip().upper()
    if mode != "VARIATIONS":
        raise ValueError(
            f"Variant operator supports only VARIATIONS mode; step mode is {mode!r}. "
            "Use run-aura for DNA/FEEDBACK."
        )

    prompt_file = step_dir / "gpt" / "outputs" / "variant_prompt.txt"
    if not prompt_file.exists():
        raise FileNotFoundError(
            f"Prompt file not found: {prompt_file}. Run run-gpt first to generate variant_prompt.txt."
        )

    effective_url = (
        url
        or designrun.get("variant_project_url")
        or read_config().get("variant_start_url")
        or "https://variant.com/projects"
    )

    out_dir = step_dir / "generators" / "variant"
    ensure_step_layout(step_dir)

    ref_images_dir = step_dir / "references" / "images"
    image_args: List[str] = []
    if ref_images_dir.exists():
        candidates = sorted(ref_images_dir.iterdir())
        for p in candidates:
            if not p.is_file():
                continue
            if p.stat().st_size >= MAX_IMAGE_SIZE_BYTES:
                continue
            image_args.append(str(p))
            if len(image_args) >= MAX_REFERENCE_IMAGES:
                break

    append_event(run_dir, "variant_started", {"step_id": step_id})

    script = get_variant_operator_script()
    cmd = [
        sys.executable,
        str(script),
        "run",
        "--url", effective_url,
        "--prompt-file", str(prompt_file),
        "--out", str(out_dir),
        "--timeout-s", str(timeout_s),
    ]
    for img in image_args:
        cmd.extend(["--image", img])
    if headed:
        cmd.append("--headed")
    if profile_dir:
        cmd.append("--profile-dir")
        cmd.append(profile_dir)
    if connect:
        cmd.append("--connect")
        cmd.append(connect)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s + 60,
            cwd=str(script.parent),
        )
    except subprocess.TimeoutExpired as e:
        append_event(run_dir, "variant_finished", {"step_id": step_id, "success": False, "error": "timeout"})
        raise RuntimeError("variant_operator timed out") from e

    if result.returncode != 0:
        append_event(run_dir, "variant_finished", {
            "step_id": step_id,
            "success": False,
            "error": (result.stderr or result.stdout or "non-zero exit")[:500],
        })
        raise RuntimeError(f"variant_operator failed: {result.stderr or result.stdout}")

    variant_project_url = None
    try:
        out = (result.stdout or "").strip()
        if out:
            parsed = json.loads(out)
            variant_project_url = parsed.get("variant_project_url")
    except Exception:
        pass
    if variant_project_url:
        update_designrun(run_dir, {"variant_project_url": variant_project_url})

    append_event(run_dir, "variant_finished", {"step_id": step_id, "success": True})
    return {"ok": True, "step_id": step_id, "variant_dir": str(out_dir), "variant_project_url": variant_project_url}


def run_variant_export_only(
    run_id: str,
    step_id: str,
    *,
    url: Optional[str] = None,
    headed: bool = False,
    profile_dir: Optional[str] = None,
    connect: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Reload the variant project at its step and export all existing outputs (links, screenshots, HTML)
    without triggering a new generation. Uses project URL from --url, step's generators/variant/url.txt,
    or designrun.json variant_project_url.
    """
    run_dir = get_run_dir(run_id)
    step_dir = get_step_dir(run_id, step_id)
    designrun = read_designrun(run_dir)

    url_txt = step_dir / "generators" / "variant" / "url.txt"
    effective_url = url or (url_txt.read_text(encoding="utf-8").strip() if url_txt.exists() else None) or designrun.get("variant_project_url")
    if not effective_url:
        raise ValueError(
            "No Variant project URL. Run run-variant first for this step, or pass --url with the project URL (variant.com/chat/...)."
        )

    out_dir = step_dir / "generators" / "variant"
    ensure_step_layout(step_dir)

    append_event(run_dir, "variant_export_only_started", {"step_id": step_id})

    script = get_variant_operator_script()
    cmd = [
        sys.executable,
        str(script),
        "export-only",
        "--url", effective_url,
        "--out", str(out_dir),
    ]
    if headed:
        cmd.append("--headed")
    if profile_dir:
        cmd.append("--profile-dir")
        cmd.append(profile_dir)
    if connect:
        cmd.append("--connect")
        cmd.append(connect)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(script.parent),
        )
    except subprocess.TimeoutExpired as e:
        append_event(run_dir, "variant_export_only_finished", {"step_id": step_id, "success": False, "error": "timeout"})
        raise RuntimeError("variant_operator export-only timed out") from e

    if result.returncode != 0:
        append_event(run_dir, "variant_export_only_finished", {
            "step_id": step_id,
            "success": False,
            "error": (result.stderr or result.stdout or "non-zero exit")[:500],
        })
        raise RuntimeError(f"variant_operator export-only failed: {result.stderr or result.stdout}")

    append_event(run_dir, "variant_export_only_finished", {"step_id": step_id, "success": True})
    try:
        out = (result.stdout or "").strip()
        if out:
            return json.loads(out)
    except Exception:
        pass
    return {"ok": True, "step_id": step_id, "variant_dir": str(out_dir), "export_only": True}


# ----------------------------
# CLI
# ----------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="designrun-manager",
        description="Main control for UI design automation: run/step state and platform script coordination.",
    )
    p.add_argument(
        "--runs-dir",
        default=None,
        help=f"Runs root directory (default: env DESIGN_RUNS_DIR or {DEFAULT_RUNS_DIR}).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # init-run
    init_p = sub.add_parser("init-run", help="Create run folder, designrun.json, events.ndjson, steps/.")
    init_p.add_argument("run_id", help="Run identifier (folder name).")

    # add-step
    add_p = sub.add_parser("add-step", help="Create step SXX_<name> with full layout.")
    add_p.add_argument("run_id", help="Run identifier.")
    add_p.add_argument("name", help="Step name (e.g. 'dna_01').")

    # set-input
    set_p = sub.add_parser("set-input", help="Write step input: user_text.txt and mode.txt.")
    set_p.add_argument("run_id", help="Run identifier.")
    set_p.add_argument("step_id", help="Step id (e.g. S01_dna_01).")
    set_p.add_argument("--user-text", default=None, help="User prompt text (or --user-text-file).")
    set_p.add_argument("--user-text-file", default=None, help="Path to file containing user prompt.")
    set_p.add_argument("--mode", choices=MODES, required=True, help="Step mode: DNA | VARIATIONS | FEEDBACK.")

    # add-references
    ref_p = sub.add_parser("add-references", help="Copy images to step references/ and write map.json.")
    ref_p.add_argument("run_id", help="Run identifier.")
    ref_p.add_argument("step_id", help="Step id.")
    ref_p.add_argument("images", nargs="+", help="Paths to reference images.")
    ref_p.add_argument("--map", default=None, help="JSON object or path to JSON: filename or path -> label.")

    # run-gpt
    gpt_p = sub.add_parser("run-gpt", help="Run gpt_operator for step; normalize outputs; update designrun.json.")
    gpt_p.add_argument("run_id", help="Run identifier.")
    gpt_p.add_argument("step_id", help="Step id.")
    gpt_p.add_argument("--url", default=None, help="ChatGPT gizmo or chat URL (else from designrun.json).")
    gpt_p.add_argument("--timeout-s", type=int, default=180, help="Timeout for completion.")
    gpt_p.add_argument("--headed", action="store_true", help="Run browser visible.")
    gpt_p.add_argument("--profile-dir", default=None, help="Chrome profile for login.")
    gpt_p.add_argument("--connect", default=None, metavar="URL", help="Attach to Chrome via CDP.")

    # run-aura
    aura_p = sub.add_parser("run-aura", help="Run aura_operator for step (DNA or FEEDBACK from mode.txt).")
    aura_p.add_argument("run_id", help="Run identifier.")
    aura_p.add_argument("step_id", help="Step id.")
    aura_p.add_argument("--url", default=None, help="DNA: start URL override. FEEDBACK: project URL if not in designrun.json.")
    aura_p.add_argument("--timeout-s", type=int, default=150, help="Timeout for generation.")
    aura_p.add_argument("--headed", action="store_true", help="Run browser visible.")
    aura_p.add_argument("--profile-dir", default=None, help="Chrome profile for login.")
    aura_p.add_argument("--connect", default=None, metavar="URL", help="Attach to Chrome via CDP.")

    # run-variant
    variant_p = sub.add_parser("run-variant", help="Run variant_operator for step (VARIATIONS mode only).")
    variant_p.add_argument("run_id", help="Run identifier.")
    variant_p.add_argument("step_id", help="Step id.")
    variant_p.add_argument("--url", default=None, help="Start or project URL (else from designrun.json or config variant_start_url).")
    variant_p.add_argument("--timeout-s", type=int, default=300, help="Timeout for 4 new outputs.")
    variant_p.add_argument("--headed", action="store_true", help="Run browser visible.")
    variant_p.add_argument("--profile-dir", default=None, help="Chrome profile for login.")
    variant_p.add_argument("--connect", default=None, metavar="URL", help="Attach to Chrome via CDP.")

    # export-variant
    export_variant_p = sub.add_parser("export-variant", help="Reload variant project at step and export all outputs (no new generation).")
    export_variant_p.add_argument("run_id", help="Run identifier.")
    export_variant_p.add_argument("step_id", help="Step id.")
    export_variant_p.add_argument("--url", default=None, help="Project URL (else from step generators/variant/url.txt or designrun.json).")
    export_variant_p.add_argument("--headed", action="store_true", help="Run browser visible.")
    export_variant_p.add_argument("--profile-dir", default=None, help="Chrome profile for login.")
    export_variant_p.add_argument("--connect", default=None, metavar="URL", help="Attach to Chrome via CDP.")

    return p


def main() -> int:
    parser = build_parser()
    ns = parser.parse_args()

    if ns.runs_dir:
        os.environ["DESIGN_RUNS_DIR"] = ns.runs_dir

    if ns.cmd == "init-run":
        init_run(ns.run_id)
        print(f"Run initialized: {get_run_dir(ns.run_id)}")
        return 0

    if ns.cmd == "add-step":
        step_id = add_step(ns.run_id, ns.name)
        print(step_id)
        return 0

    if ns.cmd == "set-input":
        if (ns.user_text is None) == (ns.user_text_file is None):
            print("Provide exactly one of --user-text or --user-text-file", file=sys.stderr)
            return 2
        user_text = ns.user_text if ns.user_text is not None else Path(ns.user_text_file).read_text(encoding="utf-8")
        set_step_input(ns.run_id, ns.step_id, user_text, ns.mode)
        print(f"Input saved for {ns.step_id}")
        return 0

    if ns.cmd == "add-references":
        map_labels = None
        if ns.map:
            s = ns.map.strip()
            if s.startswith("{"):
                map_labels = json.loads(s)
            else:
                map_labels = json.loads(Path(s).read_text(encoding="utf-8"))
        add_references(ns.run_id, ns.step_id, ns.images, map_labels)
        print(f"References saved for {ns.step_id}")
        return 0

    if ns.cmd == "run-gpt":
        try:
            result = run_gpt(
                ns.run_id,
                ns.step_id,
                url=ns.url,
                timeout_s=ns.timeout_s,
                headed=ns.headed,
                profile_dir=ns.profile_dir,
                connect=ns.connect,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        except (ValueError, FileNotFoundError, RuntimeError) as e:
            print(str(e), file=sys.stderr)
            return 1

    if ns.cmd == "run-aura":
        try:
            result = run_aura(
                ns.run_id,
                ns.step_id,
                url=ns.url,
                timeout_s=ns.timeout_s,
                headed=ns.headed,
                profile_dir=ns.profile_dir,
                connect=ns.connect,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        except (ValueError, FileNotFoundError, RuntimeError) as e:
            print(str(e), file=sys.stderr)
            return 1

    if ns.cmd == "run-variant":
        try:
            result = run_variant(
                ns.run_id,
                ns.step_id,
                url=ns.url,
                timeout_s=ns.timeout_s,
                headed=ns.headed,
                profile_dir=ns.profile_dir,
                connect=ns.connect,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        except (ValueError, FileNotFoundError, RuntimeError) as e:
            print(str(e), file=sys.stderr)
            return 1

    if ns.cmd == "export-variant":
        try:
            result = run_variant_export_only(
                ns.run_id,
                ns.step_id,
                url=ns.url,
                headed=ns.headed,
                profile_dir=ns.profile_dir,
                connect=ns.connect,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        except (ValueError, FileNotFoundError, RuntimeError) as e:
            print(str(e), file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
