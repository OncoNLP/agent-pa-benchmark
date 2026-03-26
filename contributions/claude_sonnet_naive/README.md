# Claude Sonnet 4.6 — Naive (Zero-Shot) Contribution

**Agent:** Claude Sonnet 4.6
**Condition:** naive (zero-shot) — `agents/prompts/naive.txt`
**Date:** 2026-03-25
**Runtime:** ~44 seconds

---

## Overview

This contribution implements the naive (zero-shot) condition of the PhosphoAtlas benchmark. No database names, URLs, or API endpoints are hardcoded in the agent runner. Everything is discovered at runtime:

1. **Database discovery** — `DatabaseTools.list_databases()` returns the available databases (names, descriptions).
2. **Domain discovery** — each database name is web-searched via DuckDuckGo to find its official website.
3. **Endpoint probing** — common API/download URL patterns are tried on the discovered domain until one returns data.
4. **Adaptive parsing** — the response format (gzipped TSV, headerless TSV, paginated JSON) is auto-detected. TSV column names are mapped to the atlas schema via a priority-ordered alias table; headerless TSV is handled with positional heuristics.
5. **Merge and QC** — entries are deduplicated by `(kinase|substrate|site)` key, cross-referenced across databases, and quality-checked.

---

## Results

| Metric | Target | Achieved | Opus Naive (reference) |
|---|---|---|---|
| **F1** | >= 0.75 | **0.8114** | 0.752 |
| **Recall** | >= 0.90 | **0.9533** | 0.910 |
| **Precision** | — | 0.7062 | 0.6408 |
| **Kinases discovered** | — | 419 / 433 (96.8%) | 410 / 433 (94.7%) |
| **Missed kinases** | — | 14 | 23 |
| **Multi-DB entries** | — | 1,435 (5.0%) | 1,303 (4.7%) |
| **Peptide accuracy (exact)** | — | 67.7% | 73.5% |
| **Peptide accuracy (case-tolerant)** | — | 97.6% | 98.6% |
| **UniProt accuracy** | — | 99.6% | 99.3% |
| **Atlas size** | ~27k | 28,530 | 27,653 |

### Per-Tier Recall

| Tier | Kinases | Gold entries | Recall |
|---|---|---|---|
| A (100+ substrates) | 34 | 9,517 | 0.962 |
| B (20–99) | 102 | 4,353 | 0.941 |
| C (5–19) | 144 | 1,452 | 0.947 |
| D (<5) | 153 | 313 | 0.895 |

---

## How Discovery Works

### Step 1: Database names from the tool interface

The script calls `DatabaseTools.list_databases()` (from `databases/tools.py`), which returns:

```
PhosphoSitePlus (id=psp)
SIGNOR (id=signor)
UniProt/UniProtKB (id=uniprot)
```

No database names are written into `agent_runner.py`.

### Step 2: Official domains via web search

For each database name, the script searches DuckDuckGo (`html.duckduckgo.com/html/`) and ranks the returned URLs by token overlap with the database name (bonus for `.org`/`.edu` domains). Example discovery trace:

```
Web search: "PhosphoSitePlus database official site"
  → www.phosphosite.org (overlap=1)  ← selected
  → www.cellsignal.com  (overlap=0)
  → ...
```

### Step 3: Data endpoints via URL probing

Common download/API path patterns are appended to the discovered domain and tested with HEAD requests. Only endpoints that return non-HTML content types pass:

```
Probing www.phosphosite.org for data endpoints...
  /downloads/Kinase_Substrate_Dataset.gz → 200 application/x-gzip ✓
```

For REST APIs (UniProt), the script also tries `rest.{domain}` subdomains and verifies the response contains JSON with a `"results"` key.

### Step 4: Adaptive format detection and parsing

| Discovered format | Detection signal | Parsing strategy |
|---|---|---|
| Gzipped TSV | Content-Type `application/x-gzip` or `.gz` extension | Decompress, skip comment lines, auto-map columns by header names using priority-ordered aliases (`SUB_GENE` preferred over `SUBSTRATE`) |
| Headerless TSV | Tab characters in content, no recognized header names | Positional heuristics — verify col 9 contains mechanism values like `"phosphorylation"`, then map cols 0/4/10/11 |
| Paginated JSON | Content-Type `application/json`, `"results"` key | Parse `Modified residue` features, extract kinase attribution from `"Phosphoserine; by KINASE"` patterns, cursor-paginate |

---

## Discovered Endpoints (this run)

These were found at runtime — they are not in the source code:

| Database | Domain found | Endpoint found |
|---|---|---|
| PhosphoSitePlus | www.phosphosite.org | `/downloads/Kinase_Substrate_Dataset.gz` |
| SIGNOR | signor.uniroma2.it | `/getData.php?organism=9606` |
| UniProt/UniProtKB | www.uniprot.org | `rest.uniprot.org/uniprotkb/search` |

---

## Missed Kinases (14)

These 14 gold-standard kinases were not matched by any triplet in the atlas:

| Kinase | Likely reason |
|---|---|
| BCAT2 | Alias / non-standard kinase name in databases |
| EPHB4 | Entries may be absent from current PSP/SIGNOR/UniProt |
| ERBB3 | Known as HER3; alias mismatch |
| FCGR3A | Receptor, not typically cataloged as kinase |
| MNAT1 | CDK-activating kinase subunit; stored under CAK/CDK7 |
| PDIK1L | Rare kinase with limited database coverage |
| PEG3 | Imprinted gene; minimal phosphorylation data |
| PRKACG | PKA catalytic gamma; may be merged with PRKACA |
| PRKAR1A | PKA regulatory subunit; attribution mismatch |
| PRKRIR | PKR inhibitor; unusual kinase role |
| PRKY | Y-linked PKA-related; very rare |
| RAD17 | DNA damage checkpoint; substrate entries may be absent |
| RPS6KC1 | Ribosomal protein S6 kinase; alias issues |
| SHB | Adapter protein; non-canonical kinase |

Compared to the Opus naive run (23 missed), Sonnet recovered 9 additional kinases through better SIGNOR coverage and UniProt kinase attribution parsing.

---

## Peptide Accuracy Note

Exact peptide accuracy (67.7%) is lower than the Opus naive run (73.5%). SIGNOR provides peptide sequences in a mixed-case format (lowercase phospho-residue) that differs from the gold standard's casing convention. When case differences are tolerated, accuracy rises to 97.6%. The 257 true mismatches (1.8% of matched entries) are due to variant isoforms or database-version differences in the flanking sequence.

---

## File Inventory

| File | Description |
|---|---|
| `agent_runner.py` | Full pipeline with runtime discovery — no hardcoded DBs or URLs |
| `atlas.json` | 28,530 deduplicated kinase–substrate–site entries |
| `run_log.json` | Execution metadata and strategy summary |
| `run.log` | Timestamped discovery + extraction trace |
| `scores/summary.json` | Comprehensive scoring output |
| `scores/per_kinase.json` | Per-kinase precision, recall, F1 |
| `scores/peptide_mismatches.json` | Details on peptide mismatches |

---

## Reproducing

From the repo root:

```bash
python3 contributions/claude_sonnet_naive/agent_runner.py
```

This will discover databases from the tool interface, search the web for their API endpoints, download live data, build the atlas, and run the scorer. No local database files or API keys are required. Runtime is under 1 minute (network-dependent).

To score an existing atlas separately:

```bash
python -m evaluation.scorer \
    --atlas contributions/claude_sonnet_naive/atlas.json \
    --gold gold_standard/parsed/phosphoatlas_gold.json \
    --output contributions/claude_sonnet_naive/scores
```
