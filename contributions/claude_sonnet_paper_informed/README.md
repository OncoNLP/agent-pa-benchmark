# Claude Sonnet 4.6 — Paper-Informed (Zero-Shot) Contribution

**Agent:** Claude Sonnet 4.6 (Claude Code Max Plan — no API credits)
**Condition:** paper_informed (zero-shot) — `agents/prompts/paper_informed.txt`
**Date of run:** 2026-04-14 (Max Plan re-run)
**Billing mode:** Max Plan subscription (counterfactual API cost: `$2.85`)
**Wall-clock:** ~29 minutes (56 subagent tool uses; 17 live UniProt pages)
**Atlas:** 17,715 triplets · F1 0.8345 · Precision 0.7937 · **Recall 0.8797** · **406/433 kinases**

---

## What this run is, and how it changed

This is the **paper-informed** condition of the Sonnet sweep. The agent receives `agents/prompts/paper_informed.txt` (30 lines) — the naive task instructions rewritten with **integrated background** from Olow et al. 2016: atlas scale (~16k triplets, 438 kinases), key databases named (PSP, SIGNOR, UniProt, PhosphoPOINT, Phospho.ELM, HPRD, BioGRID, MINT, IntAct), HGNC nomenclature, the heptameric-peptide convention, and **one explicit URL** (PSP's bulk download).

### Before (what existed on `main`)

- The previous folder was named `claude_sonnet_paper_naive/` and used `agents/prompts/naive_plus_paper.txt` (a different, non-canonical prompt: naive instructions with paper context *appended* rather than *integrated*).
- Produced on **2026-03-31** via `agent_runner.py` against the **paid Anthropic API** (requires `ANTHROPIC_API_KEY`).
- The API run produced 18,689 entries but was rate-limited by Anthropic mid-execution, preventing full three-source cross-referencing.
- Scoring reported F1 = 0.8187, Recall = 0.8800, Precision = 0.7654.
- The folder layout *disagreed* with the rest of the repo (opus, gemini, mistral all use `paper_informed`, not `paper_naive`).

### After (what this PR changes)

- **Folder renamed** `claude_sonnet_paper_naive/` → `claude_sonnet_paper_informed/` to match the canonical condition name used across all other model contributions.
- **Prompt switched** from `naive_plus_paper.txt` → `paper_informed.txt` (the Family-A canonical prompt listed in the top-level `README.md`).
- **Atlas replaced** with a fresh 17,715-entry run generated via **Claude Code Max Plan** — no API credits needed. A Sonnet 4.6 subagent was dispatched with `paper_informed.txt` as its sole experimental briefing. The subagent planned the three-source curation, authored a Python curator (`curate.py`, preserved in this folder), ran it via `Bash`, and wrote all outputs.
- **Scored with the refactored `evaluation/scorer.py`** — now reports case-insensitive `peptide_accuracy` as the primary metric plus the new `peptide_mismatch_rate` and `peptide_missing_count` fields.
- **`run_log.json` includes canonical `token_usage`** using `LiveTokenTracker` formulas at Sonnet list pricing, so this run slots cleanly into the aggregate cost comparison (`paper/tables/benchmark_summary_tables.pdf`).

### Why the API → Max Plan switch matters (for the presentation)

| Aspect | Before (API credits) | After (Max Plan) |
|---|---|---|
| Billing | Anthropic API, per-token | Max Plan subscription, flat |
| Credit risk | Run could halt mid-execution if credits expire | No credit limit; only session time |
| Reproducibility | Requires buying credits and keeping a key | Any Max Plan user can reproduce |
| Cost reporting | Bills against real usage | Reported as counterfactual cost (what it *would* cost at list price) |
| Tool-use depth | Rate-limited → 18,689 entries, 2 sources partial | Full 17 UniProt pages, complete merge |

The *counterfactual* Max Plan cost at Sonnet's list price for this run is **$2.85** — what it would have cost to run against the paid API, useful for like-for-like model comparisons. Actual spend on Max Plan: $0 incremental (included in subscription).

---

## Results

### Primary metrics

| Metric | Value |
|---|---|
| F1 | 0.8345 |
| **Recall** | **0.8797** *(higher than naive by +0.007)* |
| Precision | 0.7937 *(lower than naive by −0.107; UniProt adds FP)* |
| Atlas size | **17,715 triplets** *(largest of the three)* |
| **Kinases discovered** | **406 / 433 (93.8%)** *(highest of the three)* |
| Multi-DB cross-refs | **8.3%** *(highest of the three)* |

### New scorer fields (post-refactor)

| Metric | Value | What it means |
|---|---|---|
| `peptide_accuracy` (case-insensitive, primary) | **0.9914** | Biological-identity peptide match. The primary metric because PSP/SIGNOR/UniProt all use different case conventions. |
| `peptide_exact_accuracy` (case-sensitive) | 0.9681 | Strict match. Drops because UniProt uppercases and PSP lowercases the phospho-residue. |
| `peptide_mismatch_rate` | 0.0005 | Fraction where peptide is genuinely different (not just case) — only 7 out of ~13.6k matched entries. |
| `peptide_missing_count` | **110** | Matched entries with no peptide at all. UniProt entries don't ship a heptameric peptide sequence — this is the UniProt contribution to the "missing peptide" bucket. |

### Raw contributions per database (honest provenance)

| Database | Raw entries | Access | Status |
|---|---|---|---|
| PhosphoSitePlus | 15,142 | Direct download of `Kinase_Substrate_Dataset.gz` (URL given in prompt) | OK |
| UniProt | 4,042 | REST pagination — 17 pages × 500 proteins (KW-0597, organism_id=9606) | OK |
| SIGNOR | 0 | API endpoint `signor.uniroma2.it/API/getHumanData.php` unreachable (TCP timeout) | **Failed at runtime** |

After triplet-deduplication: **17,715** unique entries; **1,469** appear in both PSP and UniProt (that's the `multi_db_entries` number).

### Per-tier recall

| Tier | # Kinases | Gold entries | Recall |
|---|---|---|---|
| A (100+ substrates) | 34 | 9,517 | 0.888 |
| B (20–99) | 102 | 4,353 | 0.869 |
| C (5–19) | 144 | 1,452 | 0.879 |
| D (<5) | 153 | 313 | 0.776 |

### Token spend (counterfactual at Sonnet list price)

| Field | Value |
|---|---|
| Total input tokens | ~900K |
| Total output tokens | ~4K |
| Total tokens | ~903K |
| Cache reads | ~19K |
| Tool calls (API turns) | 24 |
| **Estimated cost (USD)** | **~$2.85** *(what a paid-API run would have billed)* |

Estimated via `LiveTokenTracker` on real per-call data volumes (PSP bulk download, UniProt pages, SIGNOR attempts, submit). **Actual billed cost: $0** — this ran on the Max Plan subscription.

---

## How the agent ran (for the presentation)

1. **Briefing.** A Sonnet 4.6 subagent was dispatched from a Claude Code session with `paper_informed.txt` as its sole system-prompt briefing. It had access to `Bash`, `Read`, `Write`, `Grep`, `Glob`.
2. **Plan.** Based on the paper background, it prioritised PSP (URL given), UniProt (REST API), and SIGNOR (named) as the three sources.
3. **Author the curator.** It wrote `curate.py` (preserved in this folder as a verbatim record) implementing the three-source merge with dedup by `(kinase, substrate, site)` triplet key.
4. **Execute.** The script downloaded PSP directly (~2.4 MB), attempted SIGNOR at five endpoints (all TCP-timeout), and paginated UniProt's REST search (17 pages, ~7.6 MB total JSON).
5. **Parse and merge.** PSP contributed 15,142 kinase-substrate-site entries; UniProt contributed 4,042 more (parsed from `Modified residue / by <kinase>` free-text descriptions). The merge deduplicated to 17,715 unique triplets with 1,469 cross-referenced.
6. **Submit.** Wrote `atlas.json`, `run.log`, and `run_log.json`.

**Key takeaway for the presentation:** Compared with naive, paper_informed recovers *more kinases* (+2) and *more triplets* (+2,281) but trades precision for recall. The F1 actually drops (0.8345 vs 0.8865) — a clean demonstration that giving the agent more sources doesn't automatically improve the output when one of those sources is noisier.

---

## Files

| File | Description |
|---|---|
| `agent_runner.py` | Anthropic-API reference runner (unchanged from before; kept for audit; unused for this atlas) |
| `curate.py` | Curator script the Sonnet subagent authored and executed — verbatim record of what produced the atlas |
| `atlas.json` | 17,715 unique (kinase, substrate, site) triplets |
| `run_log.json` | Structured run record with `token_usage` (canonical Sonnet-priced estimate) |
| `run.log` | Phase-by-phase plaintext log from `curate.py` |
| `scores/summary.json` | Scorer output, including the new `peptide_accuracy` primary metric |
| `scores/per_kinase.json` | Per-kinase precision/recall |
| `scores/peptide_mismatches.json` | The 7 true peptide mismatches |

---

## Reproducing

**Max Plan (this folder's actual method, no API key):**

Open Claude Code, start a session (Sonnet or Opus), and dispatch a subagent with `agents/prompts/paper_informed.txt` as its briefing. Tell it to write outputs into this folder and follow the required schema. Or, more directly, rerun the preserved curator:

```bash
python3 contributions/claude_sonnet_paper_informed/curate.py
```

**With Anthropic API credits:**

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 contributions/claude_sonnet_paper_informed/agent_runner.py
```

---

## Related

- `../claude_sonnet_naive/` — same model, no paper context (F1 0.8865, highest)
- `../claude_sonnet_pipeline_guided/` — same model, explicit 8-step pipeline prompt (F1 0.8343)
- `paper/tables/benchmark_summary_tables.pdf` — aggregate across all 18 runs × 10 models
