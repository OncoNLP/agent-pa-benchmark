# Claude Sonnet 4.6 — Naive + Paper Context (Zero-Shot) Contribution

**Agent:** Claude Sonnet 4.6 (autonomous via Anthropic API)
**Condition:** naive + paper context (zero-shot) — `agents/prompts/naive_plus_paper.txt`
**Date:** 2026-03-31
**Status:** Pending run (agent_runner.py ready, not yet executed to completion)

---

## Overview

This contribution runs Claude Sonnet 4.6 as a **genuinely autonomous agent** via the Anthropic API. The agent receives the naive system prompt **augmented with context from the Olow et al. 2016 PhosphoAtlas main paper**, including:

- Dataset scale (1,733 proteins, ~438 kinases, 2,617 heptameric peptides)
- The three-phase curation pipeline (Harmonize → Build relational DB → QC)
- Key databases (PSP, SIGNOR, UniProtKB, PhosphoPOINT, Phospho.ELM, HPRD, BioGRID, MINT, IntAct)
- HGNC gene symbol standardization requirements
- Heptameric peptide definition (7-residue window centered on phospho-site)
- Cross-database merging strategy (normalize kinase/substrate/site triplets)
- Exclusion of prediction-only data

The agent independently discovers database APIs, downloads data, parses entries, and submits the atlas — all through its own reasoning. The paper context provides strategic guidance but no hardcoded URLs or implementation details.

---

## Prompt Design

The prompt (`agents/prompts/naive_plus_paper.txt`) consists of two sections:

1. **Naive instructions** (identical to `agents/prompts/naive.txt`): task description, data fields, exhaustiveness requirement, cross-referencing, no fabrication
2. **Paper context appendix**: concise, non-verbatim summary of Olow et al. 2016 main paper covering dataset characteristics, curation phases, database list, HGNC naming, HPS definition, and merging strategy

This design preserves the zero-shot framing while giving the agent domain knowledge that a human curator would have after reading the paper.

---

## Results

**Pending** — The agent runner has not yet completed a full run. Results (atlas.json, run_log.json, scores/) will be populated after a successful execution.

### Expected Improvements Over Naive Baseline

The paper context should help the agent:
- Prioritize PSP, SIGNOR, and UniProt (named explicitly in the prompt)
- Use HGNC gene symbols consistently
- Understand that heptameric peptides span 3 residues on each side
- Focus on experimentally validated data only
- Apply cross-database deduplication by normalized triplets

---

## Architecture: Genuine Autonomous Agent

```
                    ┌──────────────────────┐
                    │  Claude Sonnet 4.6   │
                    │  (Anthropic API)     │
                    │                      │
                    │  Receives naive      │
                    │  prompt + Olow paper │
                    │  context. Makes ALL  │
                    │  decisions           │
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

---

## File Inventory

| File | Description |
|---|---|
| `agent_runner.py` | Autonomous agent: Anthropic API loop + tool implementations |
| `README.md` | This file |
| `atlas.json` | *(pending)* Output entries from the run |
| `run_log.json` | *(pending)* Full trace of every tool call |
| `scores/summary.json` | *(pending)* Scoring output |
| `scores/per_kinase.json` | *(pending)* Per-kinase breakdown |
| `scores/peptide_mismatches.json` | *(pending)* Peptide mismatch details |

---

## Reproducing

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export PYTHONUNBUFFERED=1
python3 contributions/claude_sonnet_paper_naive/agent_runner.py
```

Requires the `anthropic` Python package (`pip install anthropic`) and API credits (~$2-5 for a full run across all 3 databases).

---

## Known Issues

1. **Rate limiting bottleneck:** The Anthropic API rate-limits the agent during autonomous runs, causing multi-minute stalls. The retry logic uses exponential backoff (up to 75s per attempt). Python stdout buffering when piped makes progress invisible — use `PYTHONUNBUFFERED=1`.

2. **PhosphoSIGNOR parsing:** The TSV format from PhosphoSIGNOR API parsed 0 entries in initial attempts. The agent needs to discover the correct API parameters and format (JSON with embedded site encoding like `TOP2A_phSer1247`).
