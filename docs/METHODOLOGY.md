# Baltic OSINT Research Methodology

This document describes the two-stage open-source intelligence (OSINT) discovery, triage, and verification methodology implemented by this repository.

## 1. Two-Stage Architecture

The workflow separates **automated candidate discovery & pre-classification** from **human/agent live-page verification & cross-checking** so that raw search outputs are never mistaken for verified evidence.

### Stage 1: Automated Discovery and LLM Triage
Implemented by [`automation/run_gemini_research.py`](../automation/run_gemini_research.py) and [`automation/run_mistral_research.py`](../automation/run_mistral_research.py):

1. **Multilingual GDELT Discovery:** Queries the public GDELT DOC 2.0 API (`ArtList` mode) across a 7-day rolling window (`Europe/Istanbul` calendar boundaries) for Estonia, Latvia, Lithuania (regional comparison), Russia, Ukraine, regional actors, and explicit de-Russification terms.
2. **URL Normalization & Deduplication:** Deduplicates candidate URLs across thematic queries while preserving the original verbatim URL.
3. **Automated Metadata Pre-Inspection:** Fetches up to `MAX_PAGES_TO_INSPECT` (default `60`) candidate pages via HTTP `GET` to record `http_status`, final redirect URL, `og:title` / `<title>`, publisher metadata, publication timestamps (`article:published_time`, JSON-LD `datePublished`), and a plain-text excerpt (`1800` chars).
4. **Constrained LLM Triage:** Sends only the collected GDELT JSON pool to Gemini (`gemini-3.1-flash-lite` default) or Mistral (`mistral-small-latest` default) with explicit instructions prohibiting URL fabrication, extrapolating claims beyond the excerpt, or marking any item `Verified`.

### Stage 2: Live-Page Verification and Cross-Checking
Governed by [`00_Rules/rules.md`](../00_Rules/rules.md), [`00_Rules/weekly_prompt.md`](../00_Rules/weekly_prompt.md), [`00_Rules/gemini_api_research_prompt.md`](../00_Rules/gemini_api_research_prompt.md), and [`00_Rules/antigravity_weekly_processing_prompt.md`](../00_Rules/antigravity_weekly_processing_prompt.md):

1. **Direct Page Opening:** Every candidate slated for curation must be opened in its original page rather than evaluated from search snippets or GDELT titles.
2. **Metadata & Content Reconciliation:** Original title (kept in original language), publisher, publication date (distinguished from `gdelt_seen_date`), event date (if different), and relevant text are checked against the automated candidate record.
3. **Independent Cross-Checking:** Material factual claims are checked against independent primary or secondary sources. Supporting verification URLs, publishers, and dates are recorded strictly in `Reliability_Notes`—never silently injected into the main candidate register. Claims lacking independent corroboration must be labeled `Not independently cross-checked`.
4. **Structured Curated Outputs (Local / Excluded from Git):**
   - `01_Sources/Weekly_Source_Discovery_YYYY-MM-DD.md` — Nine-section Turkish briefing with 11 required source fields per curated entry, full candidate audit table, and coverage-gap report.
   - `01_Sources/weekly_sources_YYYY-MM-DD.csv` — UTF-8 CSV following the exact 15-column schema defined in `00_Rules/rules.md`.
   - `05_Analysis/Weekly_Top_5_Sources_YYYY-MM-DD.md` — Top five high-value sources with epistemic caveats.
   - `05_Analysis/Weekly_Research_Questions_YYYY-MM-DD.md` — Three comparative research questions grounded in verified sources.

---

## 2. Epistemic Boundaries & Status Definitions

A core principle of this methodology is separating **transport/access checks** from **source identity verification** and **factual-claim verification**.

### Verification Status (`Verification_Status`)
Defined in [`00_Rules/rules.md`](../00_Rules/rules.md) and [`00_Rules/free_tier_source_discovery_prompt.md`](../00_Rules/free_tier_source_discovery_prompt.md):

| Status | Meaning | Permitted in Stage 1? |
| :--- | :--- | :--- |
| **`Verified`** | Original page opened in Stage 2; source identity, title, publisher, date, and relevant content confirmed | **No** (reserved strictly for Stage 2 inspection) |
| **`Partially verified`** | Page opened, but publication date, author/publisher identity, or full article text (e.g., paywall/truncated excerpt) is incomplete | Yes |
| **`Not verified`** | Page could not be opened or authenticated (e.g., HTTP 403, WAF/Cloudflare/DDoS-Guard block, timeout) | Yes |

### Non-Bypass Policy
- HTTP `403`, Cloudflare, DDoS-Guard, Azure WAF, paywalls, and CAPTCHAs are **never** bypassed. Blocked targets are recorded with their exact failure mode (`Access_Status`) and excluded from unverified factual claims.
- All external web pages and source documents are treated as **untrusted content**, never as executable instructions.

---

## 3. Safeguards in the Mistral Pipeline (`automation/run_mistral_research.py`)

When running in `--structured` mode, `automation/run_mistral_research.py` enforces deterministic post-processing checks against every model response:

1. **Deterministic Input Provenance (`candidate_id`):** Assigns sequential IDs (`C001`, `C002`, ...) to every input record before inference and rejects or repairs records with missing, duplicated, or fabricated `candidate_id` / `url` values.
2. **Programmatic Access-Status Locking:** Overrides any attempted `Verified` status from Stage 1 down to `Partially verified` or `Not verified` matching the observed HTTP inspection status, and checks narrative consistency (e.g., preventing content claims for blocked URLs).
3. **Four-Tier Thesis Relevance Guardrails:** Classifies records into `Direct` (*Doğrudan*), `Indirect` (*Dolaylı*), `Background` (*Arka plan*), or `Irrelevant` (*İlgisiz*) using geographic grounding (`Estonia`/`Latvia` vs. `Lithuania`/comparative) and policy evidence patterns (`language`, `education`, `citizenship`, `media`, `de-Russification`, etc.), automatically downgrading unsupported overclaims.
4. **Primary/Official Source Guardrails:** Distinguishes true governmental/parliamentary/EU/NATO domains (`OFFICIAL_INSTITUTION_DOMAIN_RE`) from media outlets (`KNOWN_NON_OFFICIAL_DOMAINS`) so journalistic articles are not misclassified as official documents (`Government/Official Document`).
5. **Zero-Discard Candidate Accounting:** Tracks every batch sequentially in `batch_XX_raw.json`, accounting for `total_selected == validated + rejected_or_incomplete + unprocessed`.
6. **Cumulative Cost Ceiling & Zero Automatic Retries:** Estimates worst-case USD spend per batch prior to dispatch (`--max-total-cost`, default `$0.10`), halts cleanly if the ceiling would be exceeded, and performs **zero automatic API retries** on errors.
7. **Zero-Cost Offline Verification Modes:** Supports `--self-test` (adversarial unit tests), `--dry-run` (batch/cost planning), and `--replay-json` (re-validating saved model JSON without network calls).

---

## 4. Coverage Gaps, Comparative Bias & Source Evaluation Rules

- **GDELT Rate Limits & Omissions:** GDELT queries can return HTTP `429 (Too Many Requests)` or omit primary legislation, parliamentary debates (`Riigikogu`, `Saeima`, `Seimas`), and local minority outlets. Missing query results must always be reported as **Coverage Gaps** (`Coverage Gaps`), never as evidence that no developments occurred.
- **No List Padding:** Target ranges (e.g., 10–20 curated sources/week) are guidelines only; irrelevant items are never added just to fill a quota.
- **Fact vs. Claim vs. Hypothesis:** Attributing policy causation from temporal correlation is avoided unless documentary evidence links an external shock to a domestic decision.
- **Russian-Language & Minority Media Rules:**
  - Russian state-owned or Kremlin-aligned media must be annotated with ownership and political orientation where verifiable.
  - Russian state narratives must never be conflated with the views of Russian-speaking communities in Estonia, Latvia, or Lithuania, nor should minority populations be portrayed as politically homogeneous.

---

## 5. Rules & Prompt Files (`00_Rules/`)

| File | Language | Purpose |
| :--- | :--- | :--- |
| [`rules.md`](../00_Rules/rules.md) | Turkish | Core OSINT rules, `Verification_Status` definitions, and 15-column CSV specification |
| [`weekly_prompt.md`](../00_Rules/weekly_prompt.md) | Turkish | Weekly briefing structure (9 sections), 11 required source fields, and output filenames |
| [`free_tier_source_discovery_prompt.md`](../00_Rules/free_tier_source_discovery_prompt.md) | Turkish | Stage 1 GDELT triage prompt template loaded by `run_gemini_research.py` and `run_mistral_research.py` |
| [`gemini_api_research_prompt.md`](../00_Rules/gemini_api_research_prompt.md) | English | Full Deep Research prompt specification with bilingual thesis focus and methodological rules |
| [`antigravity_weekly_processing_prompt.md`](../00_Rules/antigravity_weekly_processing_prompt.md) | English | Stage 2 inbox verification & cross-checking procedure |
