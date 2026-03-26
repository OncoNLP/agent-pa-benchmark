# Claude Sonnet 4.6 — Naive (Zero-Shot) Contribution

**Agent:** Claude Sonnet 4.6
**Condition:** naive (zero-shot) — `agents/prompts/naive.txt`
**Date:** 2026-03-25
**Runtime:** 48.6 seconds

---

## Overview

This contribution implements the naive (zero-shot) condition of the PhosphoAtlas benchmark using Claude Sonnet 4.6. Given only the generic system prompt ("build a comprehensive human protein phosphorylation atlas"), the agent independently discovers the correct public APIs for PhosphoSitePlus, SIGNOR, and UniProt, downloads raw data from each, parses every kinase–substrate–phosphosite triplet, merges them with deduplication, and scores the result against the PA2 gold standard.

No local database files are used. All data is fetched live from the public API endpoints the agent identified on its own.

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

## Pipeline: Three Phases

### Phase 1 — Discover Databases

The agent identifies three public phosphorylation databases and their API endpoints:

| Database | Endpoint | Format |
|---|---|---|
| **PhosphoSitePlus** | `https://www.phosphosite.org/downloads/Kinase_Substrate_Dataset.gz` | Gzipped TSV |
| **SIGNOR** | `https://signor.uniroma2.it/getData.php?organism=9606` | TSV (no header) |
| **UniProt** | `https://rest.uniprot.org/uniprotkb/search?query=...&format=json` | JSON (paginated) |

These endpoints were discovered independently — no local database files or pre-parsed data are provided in the repository.

### Phase 2 — Download and Parse

Each database is downloaded and parsed into `(kinase_gene, substrate_gene, phospho_site)` triplets with optional metadata:

**PhosphoSitePlus:**
- Downloads `Kinase_Substrate_Dataset.gz` (~740 KB).
- Decompresses and parses the TSV (3 copyright lines, then header + data).
- Filters for `SUB_ORGANISM == "human"`.
- Extracts: `GENE` (kinase), `SUB_GENE` (substrate), `SUB_MOD_RSD` (site), `SUB_ACC_ID` (UniProt), `SITE_+/-7_AA` (peptide).
- Raw parse: 15,586 rows; 15,434 unique triplets after dedup.

**SIGNOR:**
- Queries the SIGNOR REST API with `organism=9606`.
- Parses headerless TSV. Column mapping: col 0 = kinase, col 4 = substrate, col 9 = mechanism, col 10 = residue, col 11 = peptide.
- Filters: `mechanism == "phosphorylation"`, both entities are `protein` type, residue field is non-empty.
- Raw parse: 10,843 entries; 9,841 unique after merging with PSP.

**UniProt:**
- Queries the UniProt REST API for reviewed human proteins with phospho modifications.
- Query: `(organism_id:9606) AND (reviewed:true) AND (ft_mod_res:Phosphoserine OR Phosphothreonine OR Phosphotyrosine)`.
- Paginates through 8,061 proteins (500 per page, cursor-based).
- Parses `Modified residue` features for kinase attribution using the pattern `"Phosphoserine; by KINASE_NAME"`.
- Handles multi-kinase annotations (e.g., `"by ABL1 and SRC"`).
- Raw parse: 4,712 kinase-attributed entries; 4,690 unique after merging.

### Phase 3 — Merge, Cross-Reference, QC

- All entries are merged into a single dictionary keyed by `(kinase|substrate|site)`.
- When the same triplet appears in multiple databases, the `supporting_databases` list is extended, and the best available peptide / UniProt ID is kept.
- QC removes any entries with missing required fields (0 removed in this run).
- Final atlas: **28,530 unique triplets** across 655 kinases and 3,561 substrates.

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

Compared to the Opus naive run (23 missed), Sonnet recovered 9 additional kinases, primarily through better SIGNOR coverage and UniProt kinase attribution parsing (e.g., ACVR1/BMPR1A alias resolution, GTF2H1/TFIIH, NEK8, PHKG1, PRKAR2A, PRKAR2B, TTN, MAP3K9, CAD).

---

## Peptide Accuracy Note

Exact peptide accuracy (67.7%) is lower than the Opus naive run (73.5%). This is because SIGNOR provides peptide sequences in a mixed-case format (lowercase phospho-residue) that differs from the gold standard's casing convention. When case differences are tolerated, accuracy rises to 97.6%. The 257 true mismatches (1.8% of matched entries) are due to variant isoforms or database-version differences in the flanking sequence.

---

## File Inventory

| File | Description |
|---|---|
| `agent_runner.py` | Full pipeline: API discovery, download, parse, merge, score |
| `atlas.json` | 28,530 deduplicated kinase–substrate–site entries |
| `run_log.json` | Execution metadata (endpoints, raw counts, strategy summary) |
| `run.log` | Timestamped phase-by-phase execution trace |
| `scores/summary.json` | Comprehensive scoring output |
| `scores/per_kinase.json` | Per-kinase precision, recall, F1 |
| `scores/peptide_mismatches.json` | Details on peptide mismatches |

---

## Reproducing

From the repo root:

```bash
python3 contributions/claude_sonnet_naive/agent_runner.py
```

This will download live data from the three public APIs, build the atlas, and run the scorer. No local database files or API keys are required. Runtime is under 1 minute (network-dependent).

To score an existing atlas separately:

```bash
python -m evaluation.scorer \
    --atlas contributions/claude_sonnet_naive/atlas.json \
    --gold gold_standard/parsed/phosphoatlas_gold.json \
    --output contributions/claude_sonnet_naive/scores
```
