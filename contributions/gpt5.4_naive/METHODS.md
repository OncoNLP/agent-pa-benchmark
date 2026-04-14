# Human Phosphorylation Atlas Build Notes

## Scope
- Human only (`organisms=9606`)
- Phosphorylation relationships only
- `~/Desktop/agent-pa-benchmark/gold_standard/sample_PA2.xlsx` used only as a formatting inspiration for the XLSX layout
- No other files from `agent-pa-benchmark` were used as source data

## Data sources used
1. **OmniPath `enzsub` web service**
   - Queried for all human enzyme-substrate PTM relationships with gene symbols and supporting source fields
2. **UniProt REST API**
   - Queried by substrate accession to retrieve protein sequences
   - Used to derive site-centered peptide windows when sequence lookup succeeded

## Curation steps
1. Query OmniPath `enzsub` for all human records with gene symbols and source-support metadata.
2. Filter to records where `modification == phosphorylation`.
3. Build phosphosite labels from `residue_type` + `residue_offset` (example: `Y15`, `S10`, `T161`).
4. Define each atlas entry as a unique `(kinase_gene, substrate_gene, phosphorylation_site, substrate_uniprot_accession)` relationship.
5. Merge duplicate relationships across supporting source databases and keep the union of all supporting databases.
6. Query UniProt for substrate protein sequences.
7. Derive a site-centered peptide window of up to +/-7 amino acids around the phosphosite when the sequence was available.
8. Export outputs as JSON, CSV, and XLSX.

## Output files
- `human_phosphorylation_atlas.json`
- `human_phosphorylation_atlas.csv`
- `human_phosphorylation_atlas.xlsx`
- `METHODS.md`

## Output fields captured
- `kinase_gene`
- `substrate_gene`
- `phosphorylation_site`
- `heptameric_peptide`
- `substrate_uniprot_accession`
- `supporting_databases`
- `references`

## Summary of this run
- Total merged phosphorylation entries: **39019**
- Unique kinase genes: **1648**
- Unique substrate genes: **3815**

## Supporting databases observed through OmniPath integration
- BEL-Large-Corpus_ProtMapper
- HPRD
- HPRD_MIMP
- KEA
- Li2012
- MIMP
- NCI-PID_ProtMapper
- PhosphoNetworks
- PhosphoSite
- PhosphoSite_MIMP
- PhosphoSite_ProtMapper
- ProtMapper
- REACH_ProtMapper
- RLIMS-P_ProtMapper
- Reactome_ProtMapper
- SIGNOR
- SIGNOR_ProtMapper
- Sparser_ProtMapper
- dbPTM
- phosphoELM
- phosphoELM_MIMP

## Limitations
- This atlas is exhaustive with respect to the records returned by the live sources used in this run, especially OmniPath’s integrated enzyme-substrate layer.
- Some database names in `supporting_databases` are upstream integrated resources exposed by OmniPath, not endpoints that were queried independently in this workflow.
- Some optional XLSX columns matching the sample style were left blank when those values were not directly available from the live sources used here.
- Peptide windows depend on successful UniProt accession-to-sequence resolution.
