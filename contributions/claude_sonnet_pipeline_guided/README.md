# Claude Sonnet 4.6 — Pipeline-Guided (Zero-Shot) Contribution

**Agent:** Claude Sonnet 4.6 (Claude Code Max Plan session)
**Condition:** pipeline_guided (zero-shot) — `agents/prompts/pipeline_guided.txt`
**Date:** 2026-04-14
**Runtime:** 84 seconds (deterministic Python parsing under agent-authored control flow)

---

## Overview

This folder is the **pipeline-guided** condition of the Sonnet sweep. The agent receives `pipeline_guided.txt` (83 lines): an explicit, step-by-step curation recipe drawn from Olow et al. 2016, organized as 3 phases / 8 steps:

- **Phase 1 — Harmonize and centralize protein data**
  1. Build Protein Reference Index (discover databases, get stats)
  2. Cross-reference external databases (list kinases/substrates with pagination)
  3. Consolidate and validate (merge aliases, remove duplicates)
- **Phase 2 — Build relational database of phosphorylation events**
  4. Systematic extraction per-kinase and per-substrate
  5. Extract and validate phospho-sites + heptameric peptides (with EXCLUSION CRITERIA: no prediction-only, no fabrication)
  6. Assemble four linked indexes into the atlas
- **Phase 3 — Cross-referencing and quality control**
  7. Multi-database cross-referencing (merge `supporting_databases` lists)
  8. Final QC: HGNC validity, dedup by triplet key, sort

### How this run was executed

Executed via **Claude Code Max Plan** — no per-token billing, no paid Anthropic API credits. The workflow:

1. Session loaded `pipeline_guided.txt` as the experimental briefing.
2. Agent authored `curate.py` in this folder — a direct translation of the 8-step pipeline into Python phases, with each step emitting a `[PHASE<n>-STEP<n>]` log line.
3. `curate.py` was executed via Bash. PSP loaded from cache (same file used by `paper_informed`), UniProt paginated live (17 pages, 500 proteins/page), SIGNOR attempted but unreachable at runtime (network timeout to `signor.uniroma2.it`).
4. Cross-referencing and QC phases ran as-scripted; `atlas.json`, `run.log`, and `run_log.json` were written.

The authored `curate.py` is preserved in the folder as a verbatim record of what was executed.

---

## Results

| Metric | Value |
|---|---|
| **F1** | 0.8343 |
| Recall | 0.8780 |
| Precision | 0.7948 |
| Kinases discovered | 404 / 433 (93.3%) |
| Atlas size | 17,655 |
| Multi-DB cross-refs | 8.1% |
| Peptide accuracy (case-insensitive) | 99.16% |

### Raw contributions per database

| Database | Raw entries contributed | Access method | Status |
|---|---|---|---|
| PhosphoSitePlus | 15,586 | Local cache (previously downloaded `Kinase_Substrate_Dataset.gz`) | OK |
| UniProt | 3,992 | REST pagination, 17 pages, keyword KW-0597, organism 9606 | OK |
| SIGNOR | 0 | API endpoint unreachable at runtime (TCP timeout to `signor.uniroma2.it`) | Failed |

After Phase 3 QC:

- **17,655** unique triplets
- **33** raw records dropped in QC (non-HGNC symbols, unparseable sites, missing fields)
- **1,436** entries confirmed by 2+ databases (multi-DB %)

### Per-tier recall

| Tier | Kinases | Gold entries | Recall |
|---|---|---|---|
| A (100+ substrates) | 34 | 9,517 | 0.888 |
| B (20–99) | 102 | 4,353 | 0.869 |
| C (5–19) | 144 | 1,452 | 0.864 |
| D (<5) | 153 | 313 | 0.776 |

---

## Interpreting this result vs. the other two conditions

| Metric | naive | paper_informed | pipeline_guided |
|---|---|---|---|
| F1 | **0.8865** | 0.8345 | 0.8343 |
| Recall | 0.8727 | **0.8797** | 0.8780 |
| Precision | **0.9007** | 0.7937 | 0.7948 |
| Kinases | 404 | **406** | 404 |
| Atlas size | 15,434 | **17,715** | 17,655 |
| Multi-DB % | 0.0% | **8.3%** | 8.1% |
| Invalid dropped in QC | — | — | **33** |

**Key observations:**

1. **Pipeline_guided and paper_informed converge** when the accessible data sources are restricted to PSP + UniProt. Their F1 scores (0.8343 vs 0.8345) and atlases (17,655 vs 17,715) are nearly identical, differing only in QC strictness. The difference between the two conditions is clearer in the *process*, not the output: pipeline_guided explicitly logs each of 8 steps and excludes 33 records for QC reasons that paper_informed does not flag.

2. **Explicit QC is visible but small.** Pipeline_guided's Phase 3 Step 8 invariants (HGNC-valid symbols, normalized sites, `{autocatalysis,similarity,predicted}` exclusion) dropped 33 entries — a 0.2% reduction. Most of the gold-standard mismatches are driven by UniProt noise that both conditions share, not by QC strictness differences.

3. **SIGNOR's offline status caps both informed conditions.** The canonical pipeline calls for SIGNOR as a third source; at runtime it was unreachable. With SIGNOR live, pipeline_guided would likely add several thousand more cross-referenced entries.

---

## Prompt structure

`pipeline_guided.txt` (83 lines) is the most verbose of the three Family-A prompts. It names the pipeline's phases and steps, provides explicit tool-call patterns (`list_databases`, `query_by_kinase`, `query_all_dbs`), lists the databases by name, states EXCLUSION CRITERIA, and reinforces the "be EXHAUSTIVE" directive with the concrete target (~16k triplets, 438 kinases).

Compare with:
- `agents/prompts/naive.txt` (20 lines, no guidance)
- `agents/prompts/paper_informed.txt` (30 lines, background + one URL)

---

## Files

| File | Description |
|---|---|
| `agent_runner.py` | Anthropic-API reference runner (requires `ANTHROPIC_API_KEY`; not used for the committed atlas) |
| `curate.py` | Curator script authored by the agent during this run — verbatim record of what was executed |
| `atlas.json` | 17,655 unique (kinase, substrate, site) triplets |
| `run_log.json` | Structured run record: databases, counts, phase-by-phase trace |
| `run.log` | Timestamped phase-by-phase log |
| `scores/summary.json` | Scorer output |
| `scores/per_kinase.json` | Per-kinase precision/recall |
| `scores/peptide_mismatches.json` | Peptide mismatch detail |

---

## Reproducing

**Max Plan (no API key):**
Open Claude Code, load `agents/prompts/pipeline_guided.txt`, have the session author + run a curator that walks the 8 steps, and write outputs to this folder. The preserved `curate.py` here is a ready-to-re-run artifact:

```bash
python3 contributions/claude_sonnet_pipeline_guided/curate.py
```

**With Anthropic API credits:**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 contributions/claude_sonnet_pipeline_guided/agent_runner.py
```

See [../claude_sonnet_naive/](../claude_sonnet_naive/) and [../claude_sonnet_paper_informed/](../claude_sonnet_paper_informed/) for the companion conditions.
