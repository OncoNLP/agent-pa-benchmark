# Claude Sonnet 4.6 — Naive + Supplement Context (Zero-Shot) Contribution

**Agent:** Claude Sonnet 4.6 (autonomous via Anthropic API)
**Condition:** naive + supplement context (zero-shot) — `agents/prompts/naive_plus_suppl.txt`
**Date:** 2026-03-31
**Status:** Pending run (agent_runner.py ready, not yet executed to completion)

---

## Overview

This contribution runs Claude Sonnet 4.6 as a **genuinely autonomous agent** via the Anthropic API. The agent receives the naive system prompt **augmented with context from the Olow et al. 2016 PhosphoAtlas Supplementary Extended Methods**, including:

- **Primary Identifier** creation: cross-linking HGNC symbols with NCBI/Entrez Gene IDs and RefSeq records
- The stepwise pipeline: STEP 1 (Protein Reference Index) → STEP 2 (Integrate 38+ databases) → STEP 3 (Harmonize/merge) → STEP 4 (Functional triage) → STEP 5 (Phospho-site extraction) → STEP 6 (QC)
- Complete database inventory (APID, BioGRID, CCDS, COSMIC, DIP, EMBL, Ensembl, GO, HPRD, KEGG, MINT, IntAct, NCBI, OMIM, PDB, Pfam, PINA, PSP, PhosphoPOINT, Phospho.ELM, STRING, UniProtKB, etc.)
- Alias tracking for cross-database protein name resolution
- QC filters: exclude prediction-only data, deduplicate by (kinase, substrate, site) triplet
- Heptameric peptide extraction from RefSeq sequences

The supplementary methods provide more granular, procedural guidance compared to the main paper context, emphasizing the stepwise pipeline and database integration mechanics.

---

## Prompt Design

The prompt (`agents/prompts/naive_plus_suppl.txt`) consists of two sections:

1. **Naive instructions** (identical to `agents/prompts/naive.txt`): task description, data fields, exhaustiveness requirement, cross-referencing, no fabrication
2. **Supplementary methods appendix**: concise, non-verbatim summary of the Olow et al. 2016 Supplementary Extended Methods, structured as 6 explicit steps (Create Reference Index → Integrate DBs → Harmonize → Functional Triage → Site Extraction → QC) plus key principles

This design tests whether procedural, step-by-step guidance from the supplement improves agent performance compared to the higher-level paper context or the naive baseline.

---

## Results

**Pending** — The agent runner has not yet completed a full run. Results (atlas.json, run_log.json, scores/) will be populated after a successful execution.

### Expected Differences from Paper-Context Condition

The supplement context emphasizes:
- **Stepwise procedure** (6 steps) vs. the paper's higher-level 3-phase description
- **Explicit database list** (38+ databases named) vs. the paper's subset
- **Alias tracking** guidance for resolving gene name discrepancies
- **Primary Identifier** concept for non-redundant protein records
- **Regex-based cross-referencing** for matching external records

---

## Architecture: Genuine Autonomous Agent

```
                    ┌──────────────────────┐
                    │  Claude Sonnet 4.6   │
                    │  (Anthropic API)     │
                    │                      │
                    │  Receives naive      │
                    │  prompt + supplement │
                    │  methods context.   │
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
python3 contributions/claude_sonnet_suppl_naive/agent_runner.py
```

Requires the `anthropic` Python package (`pip install anthropic`) and API credits (~$2-5 for a full run across all 3 databases).

---

## Known Issues

1. **Rate limiting bottleneck:** The Anthropic API rate-limits the agent during autonomous runs, causing multi-minute stalls. Use `PYTHONUNBUFFERED=1` to see real-time output.

2. **PhosphoSIGNOR parsing:** The TSV format from PhosphoSIGNOR API may parse 0 entries initially. The agent needs to discover the correct API parameters and format (JSON with embedded site encoding).
