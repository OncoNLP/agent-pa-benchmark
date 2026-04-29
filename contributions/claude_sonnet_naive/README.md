# Claude Sonnet 4.6 — Naive (Zero-Shot) Contribution

**Agent:** Claude Sonnet 4.6 (Anthropic API, original billed run)
**Condition:** naive (zero-shot) — `agents/prompts/naive.txt`
**Date of run:** 2026-03-25
**Billing mode:** Anthropic API (`$0.98` estimated at Sonnet list price)
**Wall-clock:** ~10 minutes (13 agent turns, 32 tool calls)
**Atlas:** 15,434 triplets · **F1 0.8865** · **Precision 0.9007** · Recall 0.8727 · 404/433 kinases

---

## What this run is, and how it changed

This is the **zero-shot baseline** of the three-condition Sonnet sweep. The agent was given `agents/prompts/naive.txt` (20 lines) — a task description, six required output fields, and a "be exhaustive" instruction. No database URLs. No curation strategy. No dataset scale. It had to discover everything.

### Before (what existed on `main`)

- Produced on **2026-03-25** via `agent_runner.py` against the **paid Anthropic API** (requires `ANTHROPIC_API_KEY`).
- The run billed Sonnet's published rate and ran out of credits partway through, after downloading PhosphoSitePlus but before SIGNOR/UniProt cross-referencing completed.
- This is preserved as-is because the **original run is a legitimate data point** — it captures the behaviour of a paid-API Sonnet agent under credit pressure, which is a realistic operational scenario worth benchmarking.

### After (what this PR changes)

- **No re-run for this condition** — the original atlas is unchanged. The atlas file (15,434 entries) is the same one that was already committed.
- **README rewritten** to frame the run as part of the three-condition sweep and describe it against the new evaluation metrics and token-tracking schema.
- **`run_log.json` gains a full `token_usage` block** reconstructed from the original 32-tool-call trace (`result_size` per turn) using the repo's `LiveTokenTracker` formulas (`agents/live_runner.py`). This aligns this folder with the same schema every other contribution uses (see `claude_opus_naive/run_log.json`), so the aggregate report (`paper/tables/benchmark_summary_tables.pdf`) can compare costs like-for-like.
- **Scores refreshed** using the current `evaluation/scorer.py` — same atlas, but now reports the new primary metric `peptide_accuracy` (case-insensitive match, a convention-neutral measure) alongside the secondary `peptide_exact_accuracy` (case-sensitive).

Sibling folders `claude_sonnet_paper_informed/` and `claude_sonnet_pipeline_guided/` are the *new* runs on this PR — the ones that switched from API credits to Max Plan.

---

## Results

### Primary metrics

| Metric | Value |
|---|---|
| **F1** | **0.8865** *(highest of the three conditions)* |
| Recall | 0.8727 |
| Precision | **0.9007** *(highest of the three conditions)* |
| Atlas size | 15,434 triplets |
| Kinases discovered | 404 / 433 (93.3%) |
| Multi-DB cross-refs | 0.0% (single source) |

### New scorer fields (post-refactor in `evaluation/scorer.py`)

| Metric | Value | What it means |
|---|---|---|
| `peptide_accuracy` (primary, case-insensitive) | **0.9995** | Biological identity match. Lowercase `s/t/y` vs uppercase `S/T/Y` in heptameric peptides is a database display convention (PSP lowercases phospho-capable residues; SIGNOR uppercases; UniProt varies). Same amino-acid identity either way. |
| `peptide_exact_accuracy` (secondary, case-sensitive) | 0.9758 | Strict byte-equal match. Shows how much the naive run's PSP-lowercase convention disagrees with the gold's convention. |
| `peptide_mismatch_rate` | 0.0005 | Fraction of matched entries where the peptide is genuinely different (not just case). |
| `peptide_missing_count` | 0 | Matched entries with no peptide at all. PSP always provides one, so this is zero. |

### Per-tier recall

| Tier | # Kinases | Gold entries | Recall |
|---|---|---|---|
| A (100+ substrates) | 34 | 9,517 | 0.881 |
| B (20–99) | 102 | 4,353 | 0.863 |
| C (5–19) | 144 | 1,452 | 0.870 |
| D (<5) | 153 | 313 | 0.770 |

### Token spend (at Sonnet list price `$3/$15 per 1M tokens`)

| Field | Value |
|---|---|
| Total input tokens | ~268K |
| Total output tokens | ~4K |
| Total tokens | ~272K |
| Cache reads | ~5K |
| Tool calls (API turns) | 32 |
| **Estimated cost (USD)** | **~$0.98** |

Reconstructed from the original run's trace using `LiveTokenTracker` (see `agents/live_runner.py`). Because this run billed Anthropic's API, the cost estimate is what Sonnet's list price implies for this data flow — close to what was actually charged.

---

## How the agent ran (for the presentation)

1. **Discovery.** The agent first called `list_databases()` against the benchmark's local DB harness. It discovered that `psp`, `signor`, and `uniprot` *exist as names* but the tool's `get_stats` / `list_kinases` returned zero rows (no local files are committed to the repo).
2. **Pivot to web.** With no local data, the agent switched to `web_search` to find each database's public API or bulk download. It found PSP's `Kinase_Substrate_Dataset.gz`, UniProt's REST endpoint, and attempted multiple SIGNOR URLs.
3. **Download and parse PSP.** It used `fetch_and_parse_db` to pull and parse PSP's gzipped TSV, adding 15,434 entries to the atlas.
4. **Out of credits.** While trying SIGNOR API formats, the Anthropic API balance ran out. The agent submitted the atlas built from PSP alone.

**Key takeaway for the presentation:** This is the *highest-F1* condition of the three. A single high-precision source (PSP) with near-perfect peptide fidelity beats a two-source merge (PSP + UniProt) whose free-text kinase attribution introduces false positives. Real recall-vs-precision trade-off.

---

## Files

| File | Description |
|---|---|
| `agent_runner.py` | Autonomous agent loop against the Anthropic API (reference runner; requires `ANTHROPIC_API_KEY`) |
| `atlas.json` | 15,434 unique (kinase, substrate, site) triplets |
| `run_log.json` | Full 32-call trace, now with canonical `token_usage` (Sonnet pricing) |
| `run.log` | Plaintext console output from the original run |
| `scores/summary.json` | Scorer output — includes the new `peptide_accuracy` and related fields |
| `scores/per_kinase.json` | Per-kinase precision/recall |
| `scores/peptide_mismatches.json` | Detail on the 7 true peptide mismatches |

---

## Reproducing

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 contributions/claude_sonnet_naive/agent_runner.py
```

Alternative (Max Plan, no API key): follow the workflow described in `../claude_sonnet_paper_informed/README.md` — dispatch a Sonnet subagent from Claude Code with `agents/prompts/naive.txt` as its briefing.

---

## Related

- `../claude_sonnet_paper_informed/` — same model, prompt adds paper background (F1 0.8345)
- `../claude_sonnet_pipeline_guided/` — same model, prompt specifies the 8-step pipeline (F1 0.8343)
- `paper/tables/benchmark_summary_tables.pdf` — aggregate across all 18 runs × 10 models
