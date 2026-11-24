# Baltic OSINT Research Agent

A reproducible, safeguard-driven OSINT discovery and triage toolkit for monitoring political, legislative, security, and societal developments across the Baltic states (Estonia, Latvia, Lithuania), Russia, Ukraine, and regional European actors.

> **Epistemic Design:** The Python automation layer performs **Stage 1 discovery and triage** (GDELT DOC 2.0 collection, lightweight HTML metadata inspection, and constrained LLM pre-classification). It never marks sources as `Verified` on its own—live page opening, original-document verification, and independent cross-checking remain explicit **Stage 2** tasks governed by [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) and [`00_Rules/`](00_Rules/rules.md).

---

## Repository Structure

```text
baltic-osint-research-agent-export/
├── .env.example                          # Template for API keys and model IDs (copy to automation/.env)
├── .gitignore                            # Excludes real .env files, .venv, logs, raw JSONs, and thesis outputs
├── README.md                             # Setup, CLI usage, and security overview
├── requirements.txt                      # Python dependencies
├── automation/
│   ├── run_gemini_research.py            # Stage 1 GDELT discovery + Gemini API triage
│   ├── run_mistral_research.py           # Stage 1 GDELT discovery + safeguarded batched Mistral workflow
│   └── test_mistral_connection.py        # Single-request Mistral API connectivity smoke test
├── docs/
│   └── METHODOLOGY.md                    # Two-stage OSINT methodology, verification rules & safeguard design
├── 00_Rules/
│   ├── rules.md                          # Core verification rules and 15-column CSV schema
│   ├── weekly_prompt.md                  # 9-section briefing format & 11 required source fields
│   ├── free_tier_source_discovery_prompt.md  # Prompt template used by Python discovery scripts
│   ├── gemini_api_research_prompt.md     # Deep Research prompt & quality criteria
│   └── antigravity_weekly_processing_prompt.md # Stage 2 inbox verification instructions
├── 01_Sources/.gitkeep                   # Target folder for curated weekly reports & CSVs (git-ignored)
├── 02_Raw_Data/
│   ├── Gemini_Inbox/.gitkeep             # Stage 1 Gemini raw Markdown & GDELT JSON outputs (git-ignored)
│   └── Mistral_Inbox/.gitkeep            # Stage 1 Mistral batch, audit, and Markdown outputs (git-ignored)
└── 05_Analysis/.gitkeep                  # Target folder for Top-5 & research question analyses (git-ignored)
```

---

## Prerequisites & Installation

- **Python 3.10+** (uses `zoneinfo` from the standard library and `tzdata` on Windows)
- Internet access for GDELT DOC 2.0 queries and API calls (offline modes work without internet)

1. Create and activate a virtual environment, then install dependencies:

   ```bash
   python -m venv automation/.venv
   # Windows PowerShell:
   automation/.venv/Scripts/Activate.ps1
   # Linux / macOS:
   # source automation/.venv/bin/activate

   pip install -r requirements.txt
   ```

2. Create your local environment file from `.env.example` (never commit `automation/.env`):

   ```bash
   cp .env.example automation/.env
   ```

   Edit `automation/.env` and replace `PASTE_YOUR_GEMINI_API_KEY_HERE` and/or `PASTE_YOUR_MISTRAL_API_KEY_HERE` with your own credentials.

---

## Script Usage

Run commands from the repository root (or inside `automation/`).

### 1. Offline Safeguard Self-Test (`0` API calls, `0` network calls)

Verify provenance locking, status downgrading, relevance guardrails, candidate accounting, zero-retry handling, and cost ceilings:

```bash
python automation/run_mistral_research.py --self-test
```

Expected output ends with:
```text
SELF_TEST_PASSED: All candidate_id, provenance, consistency, guardrail, accounting, zero-retry, and cost-ceiling assertions passed (0 failures).
```

### 2. Mistral Connectivity Smoke Test (1 small API call)

Sends a single `max_tokens=16` request to `ministral-3b-latest` to validate `MISTRAL_API_KEY` in `automation/.env`:

```bash
python automation/test_mistral_connection.py
```

### 3. Mistral Safeguarded Batched Triage (`automation/run_mistral_research.py`)

Plan batch layout and worst-case USD cost locally before calling the API (`--dry-run`):

```bash
python automation/run_mistral_research.py --input-json 02_Raw_Data/Gemini_Inbox/gdelt_candidates_YYYY-MM-DD.json --structured --dry-run
```

Run live structured batch triage against an existing GDELT JSON inventory (with hard cost cap and strict provenance checks):

```bash
python automation/run_mistral_research.py \
  --input-json 02_Raw_Data/Gemini_Inbox/gdelt_candidates_YYYY-MM-DD.json \
  --structured \
  --batch-size 6 \
  --max-tokens 4500 \
  --max-total-cost 0.10 \
  --stop-on-critical-provenance
```

Or collect fresh candidates from GDELT DOC 2.0 and triage in batches:

```bash
python automation/run_mistral_research.py --live-gdelt --structured --max-total-cost 0.10
```

Replay a previously saved structured JSON output locally for validation (`0` API calls):

```bash
python automation/run_mistral_research.py \
  --input-json path/to/candidates.json \
  --replay-json path/to/saved_batch_raw.json
```

All outputs are written to `02_Raw_Data/Mistral_Inbox/` with rerun-safe timestamped filenames, raw per-batch archives, an audit JSON, and a Markdown summary.

### 4. Gemini Discovery Workflow (`automation/run_gemini_research.py`)

Collects up to `35` articles per query across 7 GDELT categories (up to `90` unique candidates, inspecting up to `60` pages), writes `gdelt_candidates_YYYY-MM-DD.json`, and runs `GEMINI_MODEL` (`gemini-3.1-flash-lite` by default) using `00_Rules/free_tier_source_discovery_prompt.md`:

```bash
python automation/run_gemini_research.py
```

Outputs are saved to `02_Raw_Data/Gemini_Inbox/` (`gdelt_candidates_*.json` and `gemini_raw_*.md`). Runtime logs are saved to `automation/logs/`.

---

## Adapting to Another Research Topic

The discovery queries (`SEARCHES` in `run_gemini_research.py` and `run_mistral_research.py`) and triage prompts in `00_Rules/` are structured for comparative research on external security crises and domestic de-Russification policies in Estonia and Latvia (with Lithuania, Russia, Ukraine, and regional actors as context). To adapt the pipeline to a different OSINT domain:

1. Update the GDELT queries (`SEARCHES`) and guardrail regexes (`ESTONIA_LATVIA_EVIDENCE_RE`, `DERUSSIFICATION_POLICY_EVIDENCE_RE`, `COMPARATIVE_CONTEXT_EVIDENCE_RE`, `OFFICIAL_INSTITUTION_DOMAIN_RE`) in `automation/run_mistral_research.py`.
2. Update the rule/prompt templates in `00_Rules/`.

---

## Security & Operational Guidelines

- **Secrets:** Store credentials exclusively in `automation/.env` (git-ignored). Never commit `.env` files or paste API keys into logs or prompts. Errors in `run_mistral_research.py` and `test_mistral_connection.py` explicitly pass through `redact()` to strip secrets from diagnostics.
- **Web Safety:** Do not bypass HTTP 403 responses, WAFs, CAPTCHAs, or paywalls. Record access barriers transparently in `Access_Status` and leave affected records `Not verified`.
- **Rate Limits:** GDELT API rate-limiting (`HTTP 429`) causes missing query results and must be reported as a coverage gap rather than absence of events.

---

## Methodology Documentation

See [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) for the complete verification protocol, status definitions, safeguard specifications, and source-evaluation rules.
