# openclaw_live_best_effort

This atlas was built from live online-accessible sources during agent execution.

## Included sources
- PhosphoSIGNOR (live TSV API)

## Not fully included
- PhosphoSitePlus: bulk kinase-substrate download was license/login gated from this environment
- UniProt: live REST access was available, but a full reliable extraction of all kinase-attributed human phosphosites was not completed in this pass
- DEPOD: accessible, but phosphatase-centric and not directly suitable as a primary kinase-substrate-site corpus

## Output
- `atlas_from_phosphosignor.json` — JSON array of objects with fields:
  - `kinase_gene`
  - `substrate_gene`
  - `phospho_site`
  - `heptameric_peptide`
  - `substrate_uniprot`
  - `supporting_databases`

## Summary
- Entries: 11142
- Unique kinases: 489
- Unique substrates: 2379

This is a best-effort live atlas, not a guaranteed exhaustive human phosphorylation atlas.
