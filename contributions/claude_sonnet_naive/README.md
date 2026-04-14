# Claude Sonnet 4.6 — Naive (Zero-Shot) Contribution

**Agent:** Claude Sonnet 4.6 (autonomous via Anthropic API, original run)
**Condition:** naive (zero-shot) — `agents/prompts/naive.txt`
**Date:** 2026-03-25
**Runtime:** ~10 minutes (31 tool calls, 13 agent turns)

---

## Overview

This folder is the **zero-shot, no-context baseline** for the Sonnet sweep. The agent receives only the 20-line `naive.txt` prompt — a task description, required output fields, and an exhaustiveness instruction. No database URLs, API endpoints, curation strategy, or dataset scale are given.

The agent autonomously:

1. Called `list_databases()` / `get_stats()` / `list_kinases()` against the harness — found all local databases returned 0 entries (no local files)
2. Used `web_search` to locate PSP, SIGNOR, and UniProt endpoints
3. Used `web_fetch` / `fetch_and_parse_db` to download and parse PSP's `Kinase_Substrate_Dataset.gz`
4. Tried multiple SIGNOR API patterns and found the UniProt REST API
5. Ran out of API credits during SIGNOR exploration before completing cross-referencing

The resulting atlas is PSP-only (15,434 entries). The full tool-call trace is in `run_log.json`.

---

## Results

| Metric | Value |
|---|---|
| **F1** | **0.8865** |
| Recall | 0.8727 |
| Precision | 0.9007 |
| Kinases discovered | 404 / 433 (93.3%) |
| Atlas size | 15,434 |
| Multi-DB cross-refs | 0.0% (single source) |
| Peptide accuracy (case-insensitive) | 99.95% |
| UniProt accuracy | 99.68% |

### Per-tier recall

| Tier | Kinases | Gold entries | Recall |
|---|---|---|---|
| A (100+ substrates) | 34 | 9,517 | 0.881 |
| B (20–99) | 102 | 4,353 | 0.863 |
| C (5–19) | 144 | 1,452 | 0.870 |
| D (<5) | 153 | 313 | 0.770 |

---

## Interpreting this result

This is the **high-precision floor**. Because the naive agent found only one trusted source (PSP) before credits ran out, its atlas has essentially zero cross-referenced entries and zero false positives from noisier auxiliary sources. The resulting F1 (0.8865) is actually the **highest of the three Sonnet conditions** in this sweep — a genuine experimental finding: when rate-limit pressure forces a single-source strategy, precision dominates over recall.

See sibling folders for the informed variants:
- [`../claude_sonnet_paper_informed/`](../claude_sonnet_paper_informed/) — paper-informed (PSP + UniProt), F1 0.8345
- [`../claude_sonnet_pipeline_guided/`](../claude_sonnet_pipeline_guided/) — explicit 8-step pipeline, F1 0.8343

---

## Files

| File | Description |
|---|---|
| `agent_runner.py` | Autonomous agent loop against the Anthropic API (reference runner; requires `ANTHROPIC_API_KEY` to execute) |
| `atlas.json` | 15,434 unique (kinase, substrate, site) triplets |
| `run_log.json` | Full tool-call trace |
| `run.log` | Console output from the original run |
| `scores/summary.json` | Scorer output |
| `scores/per_kinase.json` | Per-kinase precision/recall |
| `scores/peptide_mismatches.json` | Peptide mismatch detail |

---

## Reproducing

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 contributions/claude_sonnet_naive/agent_runner.py
```

Requires `pip install anthropic` and API credits. Without credits, the Claude Code Max Plan path used for the `paper_informed` and `pipeline_guided` siblings can also be applied — see those folders for the equivalent workflow.
