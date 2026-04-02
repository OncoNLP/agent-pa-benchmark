# Phosphorylation Atlas (Kinase–Substrate–Site)

This folder contains an exhaustive export of all kinase–substrate–phosphosite relationships available from the **local databases discovered outside `agent-pa-benchmark`**, formatted to match the `sample_PA2.xlsx` schema.

## Databases discovered (local, outside agent-pa-benchmark)
- **PhosphoELM** (local dump: `phosphoELM_all_2015-04.dump`)
- **SIGNOR / PhosphoSIGNOR** (local file: `phosphosignor_kinaseALL.tsv`)
- **UniProt** mapping for accession → gene symbol (`uniprot_acc_gene.tsv`)

## Curation strategy
1. **PhosphoSIGNOR**: parse kinase → substrate phosphosite relationships from `phosphosignor_kinaseALL.tsv` (only `mechanism=phosphorylation`).
2. **PhosphoELM**: parse human-only entries from the dump, expanding multi-kinase entries into separate rows.
3. **Normalize** into the sample_PA2.xlsx column layout, including a **heptameric** peptide when a full sequence is available (PhosphoELM).
4. **Cross-reference** by merging identical kinase–substrate–site triplets and concatenating database support labels.
5. **Export** both XLSX and JSON outputs.

## Outputs
- `phosphoatlas_kinase_substrate_sites.xlsx` — final atlas in XLSX format
- `phosphoatlas_kinase_substrate_sites.json` — final atlas in JSON format (array of records)
- `build_phosphoatlas.py` — reproducible build script

## How to regenerate
```bash
. /Users/lukasamare/.openclaw/workspace/.venv/bin/activate
python /Users/lukasamare/Desktop/agent-pa-benchmark/contributions/gpt5.1/build_phosphoatlas.py
```
