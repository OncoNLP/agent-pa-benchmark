# Claude Sonnet 4.6 — Paper-Informed (Zero-Shot) Contribution

**Agent:** Claude Sonnet 4.6 (Claude Code Max Plan subagent)
**Condition:** paper_informed (zero-shot) — `agents/prompts/paper_informed.txt`
**Date:** 2026-04-14
**Runtime:** ~29 minutes wall-clock (56 tool calls inside the subagent, 17 UniProt pages)

---

## Overview

This folder is the **paper-informed** condition of the Sonnet sweep. The agent receives `paper_informed.txt` (30 lines): the naive task instructions rewritten with **integrated background** from Olow et al. 2016 — atlas scale (~16k triplets, 438 kinases), key databases named (PSP, SIGNOR, UniProt/UniProtKB, PhosphoPOINT, Phospho.ELM, HPRD, BioGRID, MINT, IntAct), HGNC nomenclature, heptameric peptide convention, and the direct PSP bulk URL.

### How this run was executed

Unlike the sibling `claude_sonnet_naive/` (which used paid Anthropic API credits), this run was produced through **Claude Code's Max Plan** — no per-token billing. The workflow:

1. A Claude Sonnet 4.6 subagent was dispatched from a Claude Code session with the paper-informed prompt as its sole experimental briefing.
2. The subagent planned a 3-source curation (PSP, SIGNOR, UniProt) based on the paper background.
3. It authored a Python curator script (`curate.py`, preserved in this folder as an execution artifact) implementing that plan, ran it via `Bash`, and wrote the outputs below.
4. SIGNOR's API was unreachable at runtime (connection timeout) — the subagent logged this as `[ERROR]` and continued with the remaining sources.

The subagent's strategy, tool calls, and downloaded-data sizes are recorded in `run_log.json`. The authored `curate.py` is left in place as a verbatim record of what was actually executed.

---

## Results

| Metric | Value |
|---|---|
| **F1** | 0.8345 |
| **Recall** | **0.8797** |
| Precision | 0.7937 |
| Kinases discovered | 406 / 433 (93.8%) |
| Atlas size | 17,715 |
| Multi-DB cross-refs | 8.3% |
| Peptide accuracy (case-insensitive) | 99.14% |
| UniProt accuracy | — (field populated from PSP / UniProt) |

### Raw contributions per database

| Database | Raw entries contributed | Access method | Status |
|---|---|---|---|
| PhosphoSitePlus | 15,142 | Direct download of `Kinase_Substrate_Dataset.gz` (URL given in prompt) | OK |
| UniProt | 4,042 | REST pagination (17 pages × 500 proteins, keyword KW-0597, organism 9606) | OK |
| SIGNOR | 0 | API endpoint unreachable at runtime | Failed |

After triplet-deduplication: **17,715** unique `(kinase, substrate, site)` entries; **1,469** appear in both PSP and UniProt.

### Per-tier recall

| Tier | Kinases | Gold entries | Recall |
|---|---|---|---|
| A (100+ substrates) | 34 | 9,517 | 0.888 |
| B (20–99) | 102 | 4,353 | 0.869 |
| C (5–19) | 144 | 1,452 | 0.879 |
| D (<5) | 153 | 313 | 0.776 |

---

## Interpreting this result vs. the naive baseline

| Metric | paper_informed | naive | Δ |
|---|---|---|---|
| F1 | 0.8345 | 0.8865 | −0.052 |
| Recall | 0.8797 | 0.8727 | +0.007 |
| Precision | 0.7937 | 0.9007 | −0.107 |
| Kinases | 406 | 404 | +2 |
| Atlas size | 17,715 | 15,434 | +2,281 |
| Multi-DB % | 8.3% | 0.0% | +8.3 pts |

The paper-informed prompt directs the agent to **two more data sources** (UniProt on top of PSP), which raises recall marginally and improves kinase coverage. However, UniProt's `Modified residue / by <kinase>` free-text annotations are noisier than PSP's strictly curated kinase-substrate pairs, so false-positive rate climbs and net F1 drops. This is a reproduction of the classic recall-vs-precision trade-off in multi-source curation.

---

## Prompt structure

`paper_informed.txt` (30 lines) rewrites the naive instructions around a BACKGROUND paragraph and key methodological bullets:

- Dataset scale: ~16k triplets, 438 kinases
- Key databases (named, not URLs): PSP, SIGNOR, UniProt/UniProtKB, PhosphoPOINT, Phospho.ELM, HPRD, BioGRID, MINT, IntAct
- HGNC gene symbol normalization
- Heptameric peptide convention (±7 AA around phospho-site)
- Multi-database cross-referencing = higher confidence
- **One explicit URL:** PhosphoSitePlus `Kinase_Substrate_Dataset.gz`

Contrast with `agents/prompts/pipeline_guided.txt` (the sibling condition) which adds a full 3-phase, 8-step procedural recipe.

---

## Files

| File | Description |
|---|---|
| `agent_runner.py` | Anthropic-API reference runner (requires `ANTHROPIC_API_KEY`; not used for the committed atlas) |
| `curate.py` | Curator script authored by the Sonnet subagent during this run — verbatim record of what was executed |
| `atlas.json` | 17,715 unique (kinase, substrate, site) triplets |
| `run_log.json` | Structured run record: databases, counts, trace |
| `run.log` | Timestamped phase-by-phase log emitted by `curate.py` |
| `scores/summary.json` | Scorer output |
| `scores/per_kinase.json` | Per-kinase precision/recall |
| `scores/peptide_mismatches.json` | Peptide mismatch detail |

---

## Reproducing

**Max Plan (no API key):**
Open Claude Code, dispatch a Sonnet subagent with `agents/prompts/paper_informed.txt` as its system prompt, tell it to write outputs to this folder, and let it execute via Bash. The agent's authored `curate.py` is preserved here as a re-runnable artifact.

**With Anthropic API credits:**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 contributions/claude_sonnet_paper_informed/agent_runner.py
```

See [../claude_sonnet_naive/](../claude_sonnet_naive/) and [../claude_sonnet_pipeline_guided/](../claude_sonnet_pipeline_guided/) for the companion conditions.
