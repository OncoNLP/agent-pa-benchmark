# Example Contribution

This folder shows the expected structure for your submission.

## Required files

- `agent_runner.py` — Your agent implementation (the code that calls the LLM API)
- `atlas.json` — Raw atlas output from your agent (JSON array of entries)
- `scores/summary.json` — Output from running the scorer

## How to generate scores

```bash
python -m evaluation.scorer \
    --atlas contributions/your_folder/atlas.json \
    --gold gold_standard/parsed/phosphoatlas_gold.json \
    --output contributions/your_folder/scores
```

## Atlas format

Your `atlas.json` should be an array of objects:

```json
[
  {
    "kinase_gene": "CDK1",
    "substrate_gene": "RB1",
    "phospho_site": "S807",
    "heptameric_peptide": "ISPLKsPYKISEG",
    "substrate_uniprot": "P06400",
    "supporting_databases": ["PSP", "SIGNOR"]
  }
]
```

Minimum required fields: `kinase_gene`, `substrate_gene`, `phospho_site`.
