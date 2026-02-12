# Design Automation

Coordinates **ChatGPT** (`gpt_operator`), **Aura.build** (`aura_operator`), and **Variant** (`variant_operator`) to automate UI design workflows. The **designrun_manager** is the main entry point -- it manages run/step layout, state, and invokes platform operators.

---

## Setup

```powershell
pip install -r requirements.txt
playwright install chromium
```

### Chrome debug mode (for `--connect`)

All operators support `--connect` to attach to an already-open Chrome instance. This is the recommended approach for staying logged in across runs.

1. Create a shortcut to Chrome with remote debugging enabled:

   ```text
   "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
   ```

2. (Optional) Add `--user-data-dir` for a dedicated automation profile:

   ```text
   "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="E:\Projects\IndieDev\design-automation\chrome-debug-profile"
   ```

3. Launch Chrome from this shortcut, log in to ChatGPT / Aura / Variant, then pass `--connect "http://127.0.0.1:9222"` to any command.

### Config (`config.json`)

Optional file at the project root for default URLs:

```json
{
  "chatgpt_url": "https://chatgpt.com/g/YOUR_GIZMO_ID-your-gizmo-name",
  "aura_start_url": "https://www.aura.build/",
  "variant_start_url": "https://variant.com/projects"
}
```

URL resolution order for each operator: `--url` flag > `designrun.json` saved URL > `config.json` default.

---

## Workflow

### 1. Create a run and step

```powershell
python designrun_manager.py init-run my-run
python designrun_manager.py add-step my-run dna_01
```

`add-step` prints the step ID (e.g. `S01_dna_01`).

### 2. Set step input

```powershell
# Inline prompt
python designrun_manager.py set-input my-run S01_dna_01 --user-text "Design a dark landing page for a web3 studio." --mode DNA

# From file
python designrun_manager.py set-input my-run S01_dna_01 --user-text-file prompt.txt --mode DNA
```

Modes: `DNA` | `VARIATIONS` | `FEEDBACK`

### 3. Add reference images (optional)

```powershell
python designrun_manager.py add-references my-run S01_dna_01 ref1.png ref2.png

# With labels
python designrun_manager.py add-references my-run S01_dna_01 ref1.png ref2.png --map "{\"ref1.png\": \"hero\", \"ref2.png\": \"footer\"}"
```

### 4. Run GPT

```powershell
python designrun_manager.py run-gpt my-run S01_dna_01 --connect "http://127.0.0.1:9222"
```

Sends the step prompt to ChatGPT, waits for the response, and writes output files under `gpt/outputs/` (`aura_dna.txt`, `variant_prompt.txt`, or `aura_edit.txt` depending on mode). Saves `chat_url` to `designrun.json` for subsequent steps.

### 5. Run Aura (DNA or FEEDBACK)

```powershell
python designrun_manager.py run-aura my-run S01_dna_01 --connect "http://127.0.0.1:9222"
```

- **DNA**: Submits `aura_dna.txt` + reference images, waits for generation, exports HTML, opens the exported HTML in a new tab for full-page screenshot capture, and saves the project URL.
- **FEEDBACK**: Navigates to the existing project URL (from `designrun.json`), submits `aura_edit.txt`, same export/capture flow.

Outputs: `generators/aura/exports/*.html`, `generators/aura/captures/*.png`, `generators/aura/url.txt`

### 6. Run Variant (VARIATIONS)

```powershell
python designrun_manager.py run-variant my-run S01_dna_01 --connect "http://127.0.0.1:9222"
```

Submits `variant_prompt.txt`, waits for 4 output cards, exports each card's shared URL and takes a full-page screenshot.

Outputs: `generators/variant/urls.json`, `generators/variant/captures/*.png`, `generators/variant/url.txt`

### 7. Re-export (screenshots only, no new generation)

```powershell
# Variant
python designrun_manager.py re-export-variant my-run S01_var_01 --connect "http://127.0.0.1:9222"
```

---

## Command Reference

### `designrun_manager.py`

All commands accept `--runs-dir <path>` (default: env `DESIGN_RUNS_DIR` or `runs`).

#### `init-run`

```
python designrun_manager.py init-run <run_id>
```

Creates the run folder with `designrun.json`, `events.ndjson`, and `steps/`.

#### `add-step`

```
python designrun_manager.py add-step <run_id> <name>
```

Creates step `SXX_<name>` under the run. Prints the step ID.

#### `set-input`

```
python designrun_manager.py set-input <run_id> <step_id> (--user-text "..." | --user-text-file <path>) --mode (DNA | VARIATIONS | FEEDBACK)
```

Saves prompt text and mode for the step.

#### `add-references`

```
python designrun_manager.py add-references <run_id> <step_id> <image> [<image> ...] [--map <json_or_path>]
```

Copies reference images into the step and writes `map.json`.

#### `run-gpt`

```
python designrun_manager.py run-gpt <run_id> <step_id> [--url <chatgpt_url>] [--headed] [--connect <cdp_url>] [--profile-dir <path>] [--timeout-s 180]
```

| Flag | Description |
|------|-------------|
| `--url` | ChatGPT gizmo/chat URL (falls back to `designrun.json` then `config.json`) |
| `--headed` | Show browser window |
| `--connect` | Attach to Chrome via CDP |
| `--profile-dir` | Chrome profile directory for persistent login |
| `--timeout-s` | Timeout in seconds (default: 180) |

#### `run-aura`

```
python designrun_manager.py run-aura <run_id> <step_id> [--url <aura_url>] [--headed] [--connect <cdp_url>] [--profile-dir <path>] [--timeout-s 150]
```

| Flag | Description |
|------|-------------|
| `--url` | DNA: start URL override. FEEDBACK: project URL if not in `designrun.json` |
| `--headed` | Show browser window |
| `--connect` | Attach to Chrome via CDP |
| `--profile-dir` | Chrome profile directory for persistent login |
| `--timeout-s` | Timeout in seconds (default: 150) |

#### `run-variant`

```
python designrun_manager.py run-variant <run_id> <step_id> [--url <variant_url>] [--headed] [--connect <cdp_url>] [--profile-dir <path>] [--timeout-s 300]
```

| Flag | Description |
|------|-------------|
| `--url` | Start or project URL (falls back to `designrun.json` then `config.json`) |
| `--headed` | Show browser window |
| `--connect` | Attach to Chrome via CDP |
| `--profile-dir` | Chrome profile directory for persistent login |
| `--timeout-s` | Timeout in seconds (default: 300) |

#### `re-export-variant`

```
python designrun_manager.py re-export-variant <run_id> <step_id> [--headed] [--connect <cdp_url>] [--profile-dir <path>]
```

Reads `result.json` version IDs, visits each `variant.com/shared/<id>`, takes screenshots. No new generation.

---

### `gpt_operator.py` (direct)

#### `run`

```
python gpt_operator.py run --url <chatgpt_url> (--prompt "..." | --prompt-file <path>) --out <dir> [--image <path>]... [--timeout-s 180] [--headed] [--connect <cdp_url>] [--profile-dir <path>]
```

#### `re-export`

```
python gpt_operator.py re-export (--designrun <designrun.json> | --url <chat_url>) --out <dir> [--settle-timeout-s 3] [--headed] [--connect <cdp_url>] [--profile-dir <path>]
```

Opens an existing ChatGPT conversation and re-captures the last assistant output.

---

### `aura_operator.py` (direct)

#### `run`

```
python aura_operator.py run --mode (DNA | FEEDBACK) --url <aura_url> --prompt-file <path> --out <dir> [--image <path>]... [--timeout-s 150] [--headed] [--connect <cdp_url>] [--profile-dir <path>]
```

#### `re-export`

```
python aura_operator.py re-export (--designrun <designrun.json> | --url <project_url>) --out <dir> [--settle-timeout-s 30] [--headed] [--connect <cdp_url>] [--profile-dir <path>]
```

Opens an existing Aura project, runs Export > Copy HTML, opens the exported HTML in a new tab for full-page screenshot capture.

---

### `variant_operator.py` (direct)

#### `run`

```
python variant_operator.py run --url <variant_url> --prompt-file <path> --out <dir> [--image <path>]... [--timeout-s 300] [--headed] [--connect <cdp_url>] [--profile-dir <path>]
```

#### `re-export`

```
python variant_operator.py re-export --out <dir> [--headed] [--connect <cdp_url>] [--profile-dir <path>]
```

Reads `result.json` in `--out` for version IDs, visits each shared URL, takes screenshots.

---

## Output Structure

```
runs/<run_id>/
  designrun.json          # Run state (chat_url, aura_project_url, variant_project_url)
  events.ndjson           # Event log
  steps/
    S01_dna_01/
      input/
        user_text.txt     # Prompt
        mode.txt          # DNA | VARIATIONS | FEEDBACK
        references/       # Reference images + map.json
      gpt/
        response.json     # Raw GPT response
        result.json       # Extraction metadata
        outputs/
          aura_dna.txt        # (DNA mode)
          aura_edit.txt       # (FEEDBACK mode)
          variant_prompt.txt  # (VARIATIONS mode)
      generators/
        aura/
          prompt_used.txt
          url.txt
          result.json
          exports/*.html
          captures/*.png
        variant/
          prompt_used.txt
          url.txt
          urls.json
          result.json
          captures/*.png
```

---

## Authentication

All operators detect login gates and support three approaches:

| Approach | How | Flags |
|----------|-----|-------|
| **CDP attach** | Start Chrome with `--remote-debugging-port=9222`, log in manually, then attach | `--connect "http://127.0.0.1:9222"` |
| **Profile dir** | Launch with a persistent Chrome profile; log in once, reuse on subsequent runs | `--profile-dir profiles/aura` `--headed` |
| **Manual login** | Run headed, log in when prompted (within timeout) | `--headed` |

Profile directories (`profiles/`, `chrome-debug-profile/`) are in `.gitignore`.

---

## Dependencies

- **playwright** -- Browser automation (Chromium)
- **Pillow** -- Image stitching for full-page scroll-capture screenshots
