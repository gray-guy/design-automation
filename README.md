# Design automation

UI design automation that coordinates ChatGPT (gpt_operator), Aura.build (aura_operator), and Variant (variant_operator). The **designrun-manager** is the main entry point: it owns run/step layout and state, and invokes platform scripts.

## Quick test: commands in order

Run these from the project root (`d:\Projects\design-automation` or where the repo is). Replace placeholders as needed.

### 1. Create a run and a step

```powershell
python designrun_manager.py init-run my-run
python designrun_manager.py add-step my-run dna_01
```

The second command prints the step id (e.g. `S01_dna_01`). Use that as `STEP_ID` below.

### 2. Set the step input (prompt + mode)

Either inline text:

```powershell
python designrun_manager.py set-input my-run S01_dna_01 --user-text "Design a dark, grid-based landing page for a web3 studio." --mode DNA
```

Or from a file:

```powershell
python designrun_manager.py set-input my-run S01_dna_01 --user-text-file prompt.txt --mode DNA
```

Modes: `DNA` | `VARIATIONS` | `FEEDBACK`.

### 3. (Optional) Add reference images

```powershell
python designrun_manager.py add-references my-run S01_dna_01 path\to\ref1.png path\to\ref2.png
```

With labels (inline JSON or path to a JSON file):

```powershell
python designrun_manager.py add-references my-run S01_dna_01 ref1.png ref2.png --map "{\"ref1.png\": \"hero\", \"ref2.png\": \"footer\"}"
```

### 4. Run GPT for the step

Set your ChatGPT gizmo (or chat) URL once in **config.json** at the project root:

```json
{
  "chatgpt_url": "https://chatgpt.com/g/YOUR_GIZMO_ID-your-gizmo-name"
}
```

Then you can run without `--url`; the manager uses, in order: `--url` (if given) → `designrun.json` `chat_url` (after the first run) → **config.json** `chatgpt_url`.

```powershell
python designrun_manager.py run-gpt my-run S01_dna_01
```

**Later steps** in the same run use `chat_url` from `designrun.json` to resume the conversation (so you can omit config after the first run if you prefer).

**With visible browser** (e.g. to log in or watch):

```powershell
python designrun_manager.py run-gpt my-run S01_dna_01 --url "https://chatgpt.com/g/..." --headed
```

**Attach to an already-open Chrome** (logged in, correct tab):

```powershell
# Start Chrome with: chrome.exe --remote-debugging-port=9222
python designrun_manager.py run-gpt my-run S01_dna_01 --url "https://chatgpt.com/g/..." --connect "http://127.0.0.1:9222"
```

After a successful run, the manager writes `runs/<run_id>/<step_id>/gpt/response.json` and `gpt/outputs/aura_dna.txt` (or variant_prompt.txt / aura_edit.txt depending on mode), and updates `designrun.json` with `chat_url` for the next run.

### 5. Run Variant (VARIATIONS)

Variant uses the step **mode** `VARIATIONS` and the prompt from `gpt/outputs/variant_prompt.txt`. Run `run-gpt` first so that file exists.

- **First run**: Start URL is `--url`, or **config.json** `variant_start_url`, or default `https://variant.com/projects`. After submit, the operator waits for a project URL (e.g. `variant.com/chat/...` or `variant.com/projects/...`), saves it to `designrun.json` (`variant_project_url`) and `generators/variant/url.txt`.
- **Later runs**: Uses `variant_project_url` from `designrun.json` (or `--url`) so the same project gets 4 new outputs per run.

The operator waits for 4 new output cards (cards with a Menu button and a new label), then for each: hover card → Menu → Copy code (saves HTML under `generators/variant/exports/`), Menu → Open in new tab (saves URL to `generators/variant/urls.json`, screenshot to `generators/variant/captures/`, then closes the tab).

```powershell
python designrun_manager.py run-variant my-run S01_dna_01 --headed
```

**With persistent login or CDP**:

```powershell
python designrun_manager.py run-variant my-run S01_dna_01 --headed --profile-dir profiles/variant
python designrun_manager.py run-variant my-run S01_dna_01 --connect "http://127.0.0.1:9222"
```

**Export only (no new generation):** To reload the project at a step and re-export all existing outputs (links, screenshots, HTML) without submitting a new prompt, use `export-variant`. The project URL is taken from the step’s `generators/variant/url.txt` or `designrun.json` (`variant_project_url`), or pass `--url`.

```powershell
python designrun_manager.py export-variant my-run S03_variation_01 --headed
```

### 6. Run Aura (DNA or FEEDBACK)

Aura uses the step **mode** from `input/mode.txt`: **DNA** (new project from `aura_dna.txt`) or **FEEDBACK** (edit existing project with `aura_edit.txt`). Ensure `run-gpt` has already produced the right file: `gpt/outputs/aura_dna.txt` for DNA, `gpt/outputs/aura_edit.txt` for FEEDBACK.

**DNA mode** (first time for a run):

- Start URL: `--url`, or **config.json** `aura_start_url`, or default `https://www.aura.build/`
- Submits `aura_dna.txt` and reference images, waits for redirect to `aura.build/editor/<id>`, then saves that URL to `designrun.json` (`aura_project_url`) and `generators/aura/url.txt`

```powershell
python designrun_manager.py run-aura my-run S01_dna_01 --headed
```

**FEEDBACK mode** (edit existing Aura project):

- Requires `aura_project_url` in `designrun.json` (from a prior DNA run) or pass `--url` with the project URL
- Submits `aura_edit.txt` and up to 2 reference images

```powershell
python designrun_manager.py run-aura my-run S01_dna_01 --headed
```

**With persistent login or CDP** (same pattern as run-gpt):

```powershell
python designrun_manager.py run-aura my-run S01_dna_01 --headed --profile-dir profiles/aura
python designrun_manager.py run-aura my-run S01_dna_01 --connect "http://127.0.0.1:9222"
```

Aura operator: detects Sign in; submits prompt + images; waits for “Generating code...” to disappear; Export → Copy HTML (saved under `generators/aura/exports/`); Hide sidebar → full-page screenshot → `generators/aura/captures/`; Show sidebar.

### 7. Inspect outputs

- Run state: `runs\my-run\designrun.json` (`chat_url`, `aura_project_url`, `variant_project_url`)
- Event log: `runs\my-run\events.ndjson`
- Step input: `runs\my-run\steps\S01_dna_01\input\user_text.txt`, `mode.txt`
- GPT raw + normalized: `runs\my-run\steps\S01_dna_01\gpt\response.json`, `gpt\outputs\aura_dna.txt` (etc.)
- Aura: `steps\S01_dna_01\generators\aura\prompt_used.txt`, `url.txt`, `exports\*.html`, `captures\*.png`
- Variant: `steps\<step_id>\generators\variant\prompt_used.txt`, `url.txt`, `urls.json`, `exports\*.html`, `captures\*.png`

---

## Command reference (order to run)

| Order | Command | Purpose |
|-------|---------|---------|
| 1 | `python designrun_manager.py init-run <run_id>` | Create run folder, designrun.json, events.ndjson, steps/ |
| 2 | `python designrun_manager.py add-step <run_id> <name>` | Create step SXX_<name>; prints step_id |
| 3 | `python designrun_manager.py set-input <run_id> <step_id> --user-text "..." \|\| --user-text-file <path> --mode DNA \|\| VARIATIONS \|\| FEEDBACK` | Save prompt and mode for the step |
| 4 (optional) | `python designrun_manager.py add-references <run_id> <step_id> <image> [<image> ...] [--map <json or path>]` | Copy refs and write map.json |
| 5 | `python designrun_manager.py run-gpt <run_id> <step_id> [--url <chatgpt url>] [--headed] [--connect <cdp url>] [--profile-dir <path>] [--timeout-s 180]` | Run ChatGPT step; URL from --url, or designrun.json, or config.json `chatgpt_url` |
| 6 (optional) | `python designrun_manager.py run-aura <run_id> <step_id> [--url <start or project url>] [--headed] [--connect <cdp url>] [--profile-dir <path>] [--timeout-s 150]` | Run Aura step (DNA or FEEDBACK from mode.txt); DNA needs aura_dna.txt, FEEDBACK needs aura_edit.txt + aura_project_url |
| 6 (optional) | `python designrun_manager.py run-variant <run_id> <step_id> [--url <start or project url>] [--headed] [--connect <cdp url>] [--profile-dir <path>] [--timeout-s 300]` | Run Variant step (VARIATIONS only); needs variant_prompt.txt; updates variant_project_url |
| 6 (optional) | `python designrun_manager.py export-variant <run_id> <step_id> [--url <project url>] [--headed] [--connect <cdp url>] [--profile-dir <path>]` | Reload variant project at step and export all outputs (links, screenshots, HTML); no new generation |

---

## Config and environment

- **config.json** (project root):
  - `chatgpt_url` – Default ChatGPT URL so `run-gpt` can run without `--url`.
  - `aura_start_url` – (Optional) Aura DNA start URL; default is `https://www.aura.build/`.
  - `variant_start_url` – (Optional) Variant start URL; default is `https://variant.com/projects`.
- **DESIGN_RUNS_DIR** – Root directory for runs (default: `runs`). Runs are stored under `<DESIGN_RUNS_DIR>/<run_id>/`.

## Scripts

- **designrun_manager.py** – Main controller: run/step layout, events, invokes gpt_operator, aura_operator, and variant_operator.
- **gpt_operator.py** – ChatGPT automation: send prompt (and images) to a gizmo/chat, wait for response, write raw + extracted blocks.
- **aura_operator.py** – Aura.build automation: DNA (new project from aura_dna.txt) and FEEDBACK (edit with aura_edit.txt); auth, submit, wait for “Generating code...” to finish, Export → Copy HTML, full-page screenshot.
- **variant_operator.py** – Variant automation: `run` (VARIATIONS: submit variant_prompt.txt, wait for 4 new outputs, export each); `export-only` (reload project URL, export all existing outputs without generating).
