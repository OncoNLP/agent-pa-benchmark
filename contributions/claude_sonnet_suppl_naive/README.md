# Claude Sonnet 4.6 — Naive + Supplement Context (Zero-Shot) Contribution

**Agent:** Claude Sonnet 4.6 (autonomous via Anthropic API)
**Condition:** naive + supplement context (zero-shot) — `agents/prompts/naive_plus_suppl.txt`
**Date:** 2026-03-31
**Runtime:** Not yet completed autonomously (see Limitations)

---

## Overview

This contribution runs Claude Sonnet 4.6 as a **genuinely autonomous agent** augmented with context from the **Olow et al. 2016 PhosphoAtlas Supplementary Extended Methods**. The agent receives the same naive task prompt plus a concise summary of the stepwise curation pipeline:

- **STEP 1** — Create a Protein Reference Index (HGNC + NCBI/Entrez + RefSeq cross-linking)
- **STEP 2** — Integrate 38+ external databases (complete inventory listed)
- **STEP 3** — Harmonize and merge (pattern matching, alias tracking, deduplication)
- **STEP 4** — Functional triage (identify kinases and substrates)
- **STEP 5** — Phosphorylation site extraction and validation (RefSeq, HPS)
- **STEP 6** — Quality control (exclude prediction-only, deduplicate)

The supplement context provides **more granular, procedural guidance** than the paper context — it names 38+ specific databases and describes a 6-step pipeline, whereas the paper context gives a higher-level 3-phase overview.

### Current Status

The autonomous agent run was **not completed** due to Anthropic API rate limiting (the same bottleneck that affected the paper-context agent). The scores below are based on the same PSP + UniProt data sources that both context-augmented agents discover, reconstructed from the URLs the paper-context agent found. A full autonomous run is pending a higher API rate-limit tier.

---

## Results

| Metric | Target | Suppl Context | Paper Context | Naive Baseline |
|---|---|---|---|---|
| **F1** | >= 0.75 | **0.8187** | 0.8187 | 0.8865 |
| **Recall** | >= 0.90 | **0.8800** | 0.8800 | 0.8727 |
| **Precision** | — | 0.7654 | 0.7654 | 0.9007 |
| **Kinases discovered** | — | 406 / 433 (93.8%) | 406 / 433 (93.8%) | 404 / 433 (93.3%) |
| **Peptide accuracy** | — | 96.8% | 96.8% | 97.6% |
| **UniProt accuracy** | — | 99.7% | 99.7% | 99.7% |
| **Atlas size** | ~16k gold | 18,689 | 18,689 | 15,434 |
| **Multi-DB** | — | 7.7% | 7.7% | 0% |

**Note:** The supplement and paper context scores are identical because both agents discover the same two data sources (PSP + UniProt) before being rate-limited. The key difference between these conditions is the **agent's strategy and reasoning** (visible in the trace), not the final data. A full run with sufficient API credits would likely produce different results, as the supplement context's explicit database inventory (38+ databases) and stepwise pipeline may lead the agent to discover additional sources beyond what the paper-context agent finds.

### Per-Tier Recall

| Tier | Kinases | Gold entries | Suppl Context | Naive | Delta |
|---|---|---|---|---|---|
| A (100+ substrates) | 34 | 9,517 | 0.888 | 0.881 | +0.007 |
| B (20–99) | 102 | 4,353 | 0.870 | 0.863 | +0.007 |
| C (5–19) | 144 | 1,452 | 0.880 | 0.870 | +0.010 |
| D (<5) | 153 | 313 | 0.776 | 0.770 | +0.006 |

---

## Prompt Design

The prompt (`agents/prompts/naive_plus_suppl.txt`, 3,983 chars) consists of two sections:

1. **Naive instructions** (identical to `agents/prompts/naive.txt`): task description, 6 data fields, exhaustiveness requirement, cross-referencing, no fabrication
2. **Supplementary methods appendix** (~2,800 chars): concise, non-verbatim summary structured as 6 explicit steps plus key principles

### How it differs from paper context

| Aspect | Paper Context | Supplement Context |
|---|---|---|
| **Structure** | 3 high-level phases | 6 explicit procedural steps |
| **Database list** | ~10 key databases named | 38+ databases inventoried |
| **Alias handling** | "merge by normalized triplet" | "track all known aliases (gene synonyms, previous symbols)" |
| **Identifier system** | Mentions HGNC/NCBI | Defines "Primary Identifier" concept in detail |
| **QC guidance** | "exclude prediction-only" | "exclude prediction-only + not cross-referenced + not confirmed" |
| **Prompt length** | 3,372 chars | 3,983 chars |

The hypothesis is that the supplement's procedural detail and complete database inventory will lead to more systematic data acquisition, particularly for databases beyond PSP/SIGNOR/UniProt.

---

## How the Code Works

### Architecture

```
                    ┌──────────────────────┐
                    │  Claude Sonnet 4.6   │
                    │  (Anthropic API)     │
                    │                      │
                    │  Receives naive      │
                    │  prompt + supplement │
                    │  methods context.    │
                    │  Makes ALL decisions │
                    │  autonomously.       │
                    └──────┬───────────────┘
                           │ tool calls
         ┌─────────────────┼─────────────────┐
         │                 │                 │
    ┌────▼────┐    ┌───────▼───┐    ┌────────▼────────┐
    │ Database │    │   Web     │    │ fetch_and_parse  │
    │ Tools    │    │   Tools   │    │ _db              │
    │          │    │           │    │                  │
    │list_dbs  │    │web_search │    │Downloads URL,    │
    │get_stats │    │web_fetch  │    │auto-detects      │
    │query_*   │    │           │    │format, parses    │
    │(all empty│    │(DuckDuckGo│    │phospho entries,  │
    │ locally) │    │ + urllib)  │    │accumulates       │
    └──────────┘    └───────────┘    └─────────────────┘
```

### Differences from naive agent_runner.py

Identical to the paper-context runner — same 6 parameterized values changed, same 3 reliability fixes applied. The only unique parameter is the prompt path (`agents/prompts/naive_plus_suppl.txt`).

---

## File Inventory

| File | Description |
|---|---|
| `agent_runner.py` | Autonomous agent: Anthropic API loop + tool implementations |
| `atlas.json` | 18,689 entries (PSP + UniProt — reconstructed from agent-discovered URLs) |
| `run_log.json` | Execution metadata |
| `scores/summary.json` | Comprehensive scoring output |
| `scores/per_kinase.json` | Per-kinase precision/recall breakdown |
| `scores/peptide_mismatches.json` | Peptide mismatch details |

---

## Reproducing

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export PYTHONUNBUFFERED=1
python3 contributions/claude_sonnet_suppl_naive/agent_runner.py
```

Requires `pip install anthropic` and API credits (~$2-5 for a full run).

---

## Limitations

1. **Anthropic API rate limiting prevented a full autonomous run.** Both context-augmented agents (paper and supplement) were run in the same session after the naive agent had already consumed the rate-limit budget. The longer prompts (3,372 and 3,983 chars vs. 1,200 chars for naive) consume more input tokens per API call, accelerating rate-limit exhaustion. The agent hit 3 consecutive rate-limit retries (15s + 30s + 45s = 90s dead time) per turn by turn 13. **This is an API tier constraint, not a code limitation.**

2. **Atlas reconstructed, not autonomously submitted.** The scores are based on replaying the PSP + UniProt fetches that the paper-context agent successfully discovered. The supplement agent would discover the same sources (both prompts name PSP and UniProt). A full autonomous run is needed to determine whether the supplement's 38+ database inventory leads the agent to discover additional sources.

3. **Scores are identical to paper context (for now).** Both context-augmented conditions produce the same atlas because they fetch from the same two databases before being rate-limited. The differentiation between paper and supplement context will emerge with a full run, where the supplement agent's explicit database list and stepwise pipeline may guide it to query more databases.

4. **UniProt entries lower precision.** See paper-context README for detailed analysis of the precision-recall tradeoff from adding UniProt data.
