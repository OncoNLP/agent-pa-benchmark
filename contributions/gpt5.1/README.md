# Phosphorylation Atlas (Kinase–Substrate–Site)

This folder contains an exhaustive export of all kinase–substrate–phosphosite relationships available in the provided `phosphoatlas_combined.json` dataset, formatted to match the `sample_PA2.xlsx` schema.

## Outputs
- `phosphoatlas_kinase_substrate_sites.xlsx` — final atlas in XLSX format
- `phosphoatlas_combined.json` — source dataset

## Columns (match `sample_PA2.xlsx`)
- KINASE GENE
- KINASE common name
- KIN_ACC_ID
- SUBSTRATE common name
- SUB_GENE_ID
- SUB_ACC_ID
- SUBSTRATE GENE
- SUB_MOD_RSD
- SITE_GRP_ID
- SITE_+/-7_AA
- BE AWARE: available in PA2_2023, or in PA1_2016_HTKAM2_only
- (blank trailing column to match sample header layout)

## How to regenerate
From the repo root (or anywhere with access to the venv):
```bash
. /Users/lukasamare/.openclaw/workspace/.venv/bin/activate
python /Users/lukasamare/Desktop/agent-pa-benchmark/contributions/gpt5.1/build_phosphoatlas.py
```

The script reads `phosphoatlas_combined.json` and writes `phosphoatlas_kinase_substrate_sites.xlsx`.
