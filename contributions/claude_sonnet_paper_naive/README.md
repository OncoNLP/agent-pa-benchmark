# Claude Sonnet 4.6 — Naive + Paper Context (Zero-Shot) Contribution

**Agent:** Claude Sonnet 4.6 (autonomous via Anthropic API)
**Condition:** naive + paper context (zero-shot) — `agents/prompts/naive_plus_paper.txt`
**Date:** 2026-03-31
**Runtime:** ~15 minutes (43 tool calls, 15 agent turns — rate-limited, see Limitations)

---

## Overview

This contribution runs Claude Sonnet 4.6 as a **genuinely autonomous agent** augmented with context from the **Olow et al. 2016 PhosphoAtlas main paper**. The agent receives the same naive task prompt plus a concise summary describing **what** the atlas looks like and **what** databases were used:

- Dataset scale (1,733 proteins, ~438 kinases, 2,617 heptameric peptides)
- The three-phase curation pipeline (Harmonize → Build relational DB → QC)
- Key databases (PSP, SIGNOR, UniProtKB, PhosphoPOINT, Phospho.ELM, HPRD, BioGRID, MINT, IntAct)
- HGNC gene symbol standardization and heptameric peptide definition
- Cross-database merging strategy and exclusion of prediction-only data

The paper context gives the agent the **"what"** — what the final product looks like and which databases matter most — but not the "how." The agent still must autonomously find download endpoints, fetch raw data, parse it, and build the atlas. No URLs or API endpoints are provided.

### What the agent did differently from naive

With paper context, the agent:

1. Immediately prioritized PSP, SIGNOR, and UniProt (named in the prompt) instead of discovering them through trial-and-error
2. **Successfully queried UniProt REST API** for phosphoserine, phosphothreonine, and phosphotyrosine entries (the naive agent never reached UniProt)
3. Accumulated **18,689 entries from 2 databases** (PSP + UniProt) before being rate-limited, vs. the naive agent's 15,434 from PSP alone
4. Achieved **7.7% multi-database cross-referencing** (vs. 0% for naive)
5. Was rate-limited at turn 15 while attempting to fetch SIGNOR data

---

## Results

| Metric | Target | Paper Context | Naive Baseline | Delta |
|---|---|---|---|---|
| **F1** | >= 0.75 | **0.8187** | 0.8865 | -0.068 |
| **Recall** | >= 0.90 | **0.8800** | 0.8727 | +0.007 |
| **Precision** | — | 0.7654 | 0.9007 | -0.135 |
| **Kinases discovered** | — | 406 / 433 (93.8%) | 404 / 433 (93.3%) | +2 |
| **Peptide accuracy** | — | 96.8% | 97.6% | -0.8% |
| **UniProt accuracy** | — | 99.7% | 99.7% | same |
| **Atlas size** | ~16k gold | 18,689 | 15,434 | +3,255 |
| **Multi-DB** | — | 7.7% | 0% | +7.7% |

### Key Takeaway

The paper context **improved recall** (+0.7%) and **kinase discovery** (+2 kinases) by guiding the agent to query UniProt in addition to PSP. However, the UniProt entries introduced **more false positives** (4,218 vs. 1,505), lowering precision and overall F1. UniProt's "Modified residue" annotations include entries parsed from `by KINASE` free-text descriptions, which are noisier than PSP's curated kinase-substrate pairs.

### Per-Tier Recall

| Tier | Kinases | Gold entries | Paper Context | Naive | Delta |
|---|---|---|---|---|---|
| A (100+ substrates) | 34 | 9,517 | 0.888 | 0.881 | +0.007 |
| B (20–99) | 102 | 4,353 | 0.870 | 0.863 | +0.007 |
| C (5–19) | 144 | 1,452 | 0.880 | 0.870 | +0.010 |
| D (<5) | 153 | 313 | 0.776 | 0.770 | +0.006 |

Recall improved uniformly across all tiers, with the largest gain in Tier C (rare kinases with 5–19 substrates).

---

## Prompt Design

The prompt (`agents/prompts/naive_plus_paper.txt`, 3,372 chars) consists of two sections:

1. **Naive instructions** (identical to `agents/prompts/naive.txt`): task description, 6 data fields, exhaustiveness requirement, cross-referencing, no fabrication
2. **Paper context appendix** (~2,100 chars): concise, non-verbatim summary of Olow et al. 2016 covering dataset characteristics, curation phases, database list, HGNC naming, HPS definition, and merging strategy

This preserves the zero-shot framing (no examples, no hardcoded URLs) while giving the agent the same domain knowledge a human curator would have after reading the paper.

---

## Agent Behavior Trace

**Turns 1–4:** Database discovery and initial exploration
- Called `list_databases()`, `get_stats()`, `list_kinases()` — all empty locally
- Immediately searched for PSP, SIGNOR, and UniProt download URLs (guided by prompt)

**Turns 5–8:** Found and explored database endpoints
- Discovered PSP download page, PhosphoSIGNOR API, UniProt REST API docs
- Fetched PhosphoSIGNOR TSV (parsed 0 entries — format mismatch)

**Turns 9–12:** Successful data acquisition
- **PSP:** Downloaded `Kinase_Substrate_Dataset.gz` — 15,434 entries
- **UniProt pSer:** Paginated REST API — 4,348 entries (8,149 parsed, deduplicated)
- **UniProt pThr:** Paginated REST API — 3,223 entries
- **UniProt pTyr:** Paginated REST API — 1,769 entries
- Total accumulated: **18,689 unique entries**

**Turns 13–15:** Attempted SIGNOR (rate-limited)
- Tried multiple SIGNOR endpoints and a HuggingFace mirror
- Hit repeated Anthropic API rate limits (3 attempts per turn)
- Agent was still making progress when terminated

---

## How the Code Works

### Architecture

```
                    ┌──────────────────────┐
                    │  Claude Sonnet 4.6   │
                    │  (Anthropic API)     │
                    │                      │
                    │  Receives naive      │
                    │  prompt + Olow paper │
                    │  context summary.    │
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

The agent_runner.py is identical to the naive version except for **6 parameterized values**:

| Parameter | Naive | Paper Context |
|---|---|---|
| Docstring | "Naive Agent Runner" | "Paper-Context Agent Runner" |
| Usage command | `claude_sonnet_naive/` | `claude_sonnet_paper_naive/` |
| Output directory | `contributions/claude_sonnet_naive` | `contributions/claude_sonnet_paper_naive` |
| Prompt file | `agents/prompts/naive.txt` | `agents/prompts/naive_plus_paper.txt` |
| Condition label | "naive (zero-shot)" | "naive + paper context (zero-shot)" |
| Run log prompt field | "naive (zero-shot)" | "naive + paper context (zero-shot)" |

Additionally, three reliability fixes were applied (not in the naive version):
- `_log()` helper with `flush=True` for visible output when piped
- 300s timeout on the Anthropic API client
- 2s inter-turn delay to reduce rate-limit hits

All tool implementations, parsers, deduplication logic, and scoring are identical.

---

## File Inventory

| File | Description |
|---|---|
| `agent_runner.py` | Autonomous agent: Anthropic API loop + tool implementations |
| `atlas.json` | 18,689 entries (PSP + UniProt — rate-limited before SIGNOR) |
| `run_log.json` | Execution metadata and trace |
| `scores/summary.json` | Comprehensive scoring output |
| `scores/per_kinase.json` | Per-kinase precision/recall breakdown |
| `scores/peptide_mismatches.json` | Peptide mismatch details |

---

## Reproducing

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export PYTHONUNBUFFERED=1
python3 contributions/claude_sonnet_paper_naive/agent_runner.py
```

Requires `pip install anthropic` and API credits (~$2-5 for a full run).

---

## Limitations

1. **Anthropic API rate limiting is the primary bottleneck.** The agent was rate-limited starting at turn 9 and hit 3 consecutive rate-limit retries per turn by turn 13. Each retry cycle adds 15–45 seconds of dead time. This is an API tier issue, not a code issue — the naive agent (run earlier with a fresh credit window) completed without severe rate limiting. The paper and supplement agents, run later in the same billing period, exhausted the rate-limit budget faster due to their longer prompts consuming more tokens per API call.

2. **UniProt entries are noisier than PSP.** The UniProt "Modified residue" annotations are parsed from free-text descriptions like "Phosphoserine; by CDK1". This regex-based extraction introduces false positives (novel kinase names like "AUTOCATALYSIS", "B1") that PSP's curated pairs do not have. This explains the precision drop from 0.90 to 0.77.

3. **SIGNOR was not reached.** The agent discovered PhosphoSIGNOR API endpoints and a HuggingFace mirror but was unable to parse SIGNOR data before being rate-limited. Adding SIGNOR would likely improve recall further while maintaining precision (SIGNOR is curated).

4. **Atlas was reconstructed from agent-discovered URLs.** The agent accumulated 18,689 entries in memory but was killed before calling `submit_atlas`. The atlas.json was generated by replaying the same PSP + UniProt fetches the agent successfully completed.
