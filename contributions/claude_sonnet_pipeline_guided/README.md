# Claude Sonnet 4.6 — Pipeline-Guided (Zero-Shot) Contribution

**Agent:** Claude Sonnet 4.6 (Claude Code Max Plan — no API credits)
**Condition:** pipeline_guided (zero-shot) — `agents/prompts/pipeline_guided.txt`
**Date of run:** 2026-04-14 (Max Plan re-run)
**Billing mode:** Max Plan subscription (counterfactual API cost: `$3.24`)
**Wall-clock:** 84 seconds (1 planner + 2 discovery + 1 PSP fetch + 1 SIGNOR fail + 17 UniProt pages + 3 QC/submit = 25 turns)
**Atlas:** 17,655 triplets · F1 0.8343 · Precision 0.7948 · Recall 0.8780 · 404/433 kinases · 33 records dropped by Phase-3 QC

---

## What this run is, and how it changed

This is the **pipeline-guided** condition of the Sonnet sweep. The agent receives `agents/prompts/pipeline_guided.txt` (83 lines) — the most detailed of the three prompts. It lays out an explicit **3-phase, 8-step curation pipeline** drawn directly from Olow et al. 2016, with named tools, exclusion criteria, and a reinforced "be exhaustive" directive. This is the only Family-A prompt that tells the agent *how* to curate, not just *what* to curate.

### Before (what existed on `main`)

- The previous folder was named `claude_sonnet_suppl_naive/` and used `agents/prompts/naive_plus_suppl.txt` (a Family-B variant: naive instructions with the paper's *supplementary methods* appended verbatim). That prompt is subtly different from `pipeline_guided.txt` — it hands the agent a 6-step recipe rather than an 8-step one, and lists 38+ databases inventory-style without tool-call guidance.
- Produced on **2026-03-31** via `agent_runner.py` against the **paid Anthropic API**.
- The API run's atlas and scores happened to match the paper-naive run byte-for-byte (both hit rate limits at the same point, producing identical 18,689-entry output).
- The folder layout was inconsistent with opus/gemini/mistral, which all use `pipeline_guided`.

### After (what this PR changes)

- **Folder renamed** `claude_sonnet_suppl_naive/` → `claude_sonnet_pipeline_guided/` to match the canonical Family-A condition name.
- **Prompt switched** from `naive_plus_suppl.txt` → `pipeline_guided.txt` — the actual Family-A prompt that the condition is supposed to be testing.
- **Atlas regenerated** via **Claude Code Max Plan** (no API credits). The session authored `curate.py` (preserved here) as a direct translation of the 8-step pipeline into Python phases. Each step emits a `[PHASE<n>-STEP<n>]` line in `run.log`, so the execution is auditable.
- **Scored with refactored `evaluation/scorer.py`** — includes the new case-insensitive `peptide_accuracy` primary metric plus `peptide_mismatch_rate` and `peptide_missing_count`.
- **`run_log.json` includes canonical `token_usage`** using `LiveTokenTracker` at Sonnet pricing for cross-model cost comparison.

### Why the API → Max Plan switch matters (for the presentation)

Same story as the paper_informed sibling: flat Max Plan subscription replaces per-token API billing, which removes the credit-limit risk that previously capped these runs. Counterfactual cost is **$3.24** at Sonnet list pricing — this is what a paid-API run of the same pipeline would have billed. Actual Max Plan spend: $0 incremental.

---

## Results

### Primary metrics

| Metric | Value |
|---|---|
| F1 | 0.8343 |
| Recall | 0.8780 |
| Precision | 0.7948 |
| Atlas size | 17,655 triplets |
| Kinases discovered | 404 / 433 (93.3%) |
| Multi-DB cross-refs | 8.1% |
| **Records dropped in QC** | **33** *(the only condition that explicitly drops records)* |

### New scorer fields (post-refactor)

| Metric | Value | What it means |
|---|---|---|
| `peptide_accuracy` (case-insensitive, primary) | **0.9916** | Biological identity peptide match. |
| `peptide_exact_accuracy` (case-sensitive) | 0.9683 | Strict match; drops for the same PSP-vs-UniProt case-convention reason as paper_informed. |
| `peptide_mismatch_rate` | 0.0005 | Only 7 truly different peptides in ~13.7k matched entries. |
| `peptide_missing_count` | 107 | UniProt entries without a peptide sequence — similar to paper_informed's 110. |

### Raw contributions per database (honest provenance)

| Database | Raw entries | Access | Status |
|---|---|---|---|
| PhosphoSitePlus | 15,586 | Loaded from local cache (decompressed from the same `Kinase_Substrate_Dataset.gz` used by `paper_informed`) | OK |
| UniProt | 3,992 | REST pagination — 17 pages × 500 proteins (KW-0597, organism_id=9606) | OK |
| SIGNOR | 0 | API endpoint unreachable at runtime (TCP timeout to `signor.uniroma2.it`) | **Failed at runtime** |

Phase 3 Step 8 QC explicitly dropped **33** records (non-HGNC-looking symbols, unparseable sites, autocatalysis/similarity/predicted-only annotations). This is the primary behavioural difference versus `paper_informed`, which keeps all parsed entries.

### Per-tier recall

| Tier | # Kinases | Gold entries | Recall |
|---|---|---|---|
| A (100+ substrates) | 34 | 9,517 | 0.888 |
| B (20–99) | 102 | 4,353 | 0.869 |
| C (5–19) | 144 | 1,452 | 0.864 |
| D (<5) | 153 | 313 | 0.776 |

### Token spend (counterfactual at Sonnet list price)

| Field | Value |
|---|---|
| Total input tokens | ~1.03M |
| Total output tokens | ~5K |
| Total tokens | ~1.03M |
| Cache reads | ~35K |
| Tool calls (API turns) | 25 |
| **Estimated cost (USD)** | **~$3.24** *(what a paid-API run would have billed)* |

Slightly higher than `paper_informed` (~$2.85) because the pipeline-guided prompt is longer (83 lines vs 30), so every cache read costs more, and the execution includes additional discovery and QC turns. **Actual billed cost: $0** — ran on Max Plan subscription.

---

## How the agent ran (for the presentation)

The pipeline prompt maps directly to three phases, each logged by `curate.py`:

**Phase 1 — Harmonize and centralize protein data**
1. **Discover databases.** Checked local `databases/` cache, fell back to public APIs.
2. **Cross-reference external databases.** PSP (cached), SIGNOR (attempted, failed), UniProt (paginated live).
3. **Consolidate.** Merged by `(kinase, substrate, site)` triplet during parsing.

**Phase 2 — Build relational database of phosphorylation events**
- **Steps 4-5.** Systematic extraction with residue+position normalization, heptameric peptide retention, and exclusion of autocatalysis/similarity/prediction-only annotations per the paper's exclusion criteria.
- **Step 6.** Atlas assembly — 17,655 unique triplets; 33 records dropped as invalid.

**Phase 3 — Cross-referencing and quality control**
7. **Multi-DB cross-referencing.** Merged `supporting_databases` lists for identical triplets → 1,436 multi-DB entries.
8. **Final QC.** HGNC validity, site normalization, dedup, deterministic sort.

**Key takeaway for the presentation:** When SIGNOR is offline (as it was at runtime), `pipeline_guided` and `paper_informed` converge to nearly identical results (F1 0.8343 vs 0.8345) — the difference shifts from the *atlas* to the *process*. Pipeline_guided's explicit QC step drops 33 records that paper_informed keeps; both converge on the same ~17.7k-triplet output because PSP + UniProt are the only reachable sources.

The scientific finding: more elaborate prompting doesn't overcome data-source limitations. When SIGNOR is live, pipeline_guided should pull ahead because of its explicit third-source mandate and cross-referencing emphasis.

---

## Files

| File | Description |
|---|---|
| `agent_runner.py` | Anthropic-API reference runner (kept for audit; unused for this atlas; prompt path updated to `pipeline_guided.txt`) |
| `curate.py` | Curator script the agent authored and executed — mirrors the 8-step pipeline one-to-one |
| `atlas.json` | 17,655 unique (kinase, substrate, site) triplets |
| `run_log.json` | Structured run record with canonical `token_usage` (Sonnet-priced estimate) |
| `run.log` | Phase-by-phase plaintext log (each step stamped with `[PHASE<n>-STEP<n>]`) |
| `scores/summary.json` | Scorer output, including the new `peptide_accuracy` primary metric |
| `scores/per_kinase.json` | Per-kinase precision/recall |
| `scores/peptide_mismatches.json` | The 7 true peptide mismatches |

---

## Reproducing

**Max Plan (this folder's actual method, no API key):**

```bash
python3 contributions/claude_sonnet_pipeline_guided/curate.py
```

**With Anthropic API credits:**

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 contributions/claude_sonnet_pipeline_guided/agent_runner.py
```

---

## Related

- `../claude_sonnet_naive/` — zero-context baseline (F1 0.8865, highest)
- `../claude_sonnet_paper_informed/` — prompt adds paper background but no pipeline (F1 0.8345)
- `paper/tables/benchmark_summary_tables.pdf` — aggregate across all 18 runs × 10 models
