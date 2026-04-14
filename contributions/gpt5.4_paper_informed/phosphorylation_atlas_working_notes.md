# Human Phosphorylation Atlas Working Notes

## Source discovery and reachability

| Source | Endpoint tested | Reachability | Human filtering | Exact kinase-substrate-site extraction | Provenance exposed | Human site-specific rows retrieved | Notes |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| OmniPath enzsub | https://omnipathdb.org/enzsub?format=json&organisms=9606&genesymbols=1&fields=sources,references,curation_effort | reachable | yes (`organisms=9606`) | yes | yes (`sources`) | 39037 | Used as the primary integrated live source. |
| UniProt REST | https://rest.uniprot.org/uniprotkb/search | reachable | yes (`organism_id:9606`) | sequence support only | no | 3952 accessions resolved | Used for accession normalization and validated peptide derivation. |

## Processing summary

- Filtered to human phosphorylation records only.
- Eligible rows required kinase, substrate, and exact phosphosite.
- Duplicates were merged strictly by exact triplet `(kinase_gene, substrate_gene, phospho_site)`.
- `supporting_databases` is the union of exact-support sources reported for that triplet.
- `substrate_uniprot` is included when a trustworthy accession was present.
- `heptameric_peptide` was only included when the UniProt sequence residue matched the reported phosphosite.

## Counts

- Human phosphorylation records seen from primary source: 39037
- Merged atlas triplets: 39015
- Unique kinase genes: 1648
- Unique substrate genes: 3815

## Source support counts reported through OmniPath

- BEL-Large-Corpus_ProtMapper: 905
- HPRD: 2680
- HPRD_MIMP: 9022
- KEA: 10999
- Li2012: 322
- MIMP: 12539
- NCI-PID_ProtMapper: 1671
- PhosphoNetworks: 4212
- PhosphoSite: 12889
- PhosphoSite_MIMP: 11786
- PhosphoSite_ProtMapper: 9206
- ProtMapper: 20863
- REACH_ProtMapper: 5099
- RLIMS-P_ProtMapper: 2145
- Reactome_ProtMapper: 989
- SIGNOR: 7607
- SIGNOR_ProtMapper: 7280
- Sparser_ProtMapper: 5427
- dbPTM: 1271
- phosphoELM: 1788
- phosphoELM_MIMP: 8920

## Limitations

- This run relies on live, reachable sources queried during execution.
- Some upstream database names appear as provenance via OmniPath rather than direct standalone retrievals in this script.
- Blank XLSX fields were left blank rather than fabricated.
