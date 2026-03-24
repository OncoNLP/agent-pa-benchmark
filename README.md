# PhosphoAtlas Agent Benchmark

Benchmarking AI agents on their ability to curate a comprehensive human protein phosphorylation atlas, evaluated against PhosphoAtlas 2.0 as the gold standard.

## For Students: Getting Started

### What you need

1. **The prompt** — start with `agents/prompts/naive.txt`. This is the system prompt your agent receives.
2. **The gold standard** — `gold_standard/parsed/phosphoatlas_gold.json` (already in this repo). This is what your agent's output is scored against.
3. **The scorer** — `evaluation/scorer.py` scores your agent's output.

### Step-by-step

```bash
# 1. Clone and install
git clone https://github.com/OncoNLP/agent-pa-benchmark.git
cd agent-pa-benchmark
pip install -r requirements.txt

# 2. Build and run your agent (your own code)
#    Your agent should produce a JSON file — an array of objects like:
#    [
#      {
#        "kinase_gene": "CDK1",
#        "substrate_gene": "RB1",
#        "phospho_site": "S807",
#        "heptameric_peptide": "...",       (optional)
#        "substrate_uniprot": "P06400",     (optional)
#        "supporting_databases": ["PSP"]    (optional)
#      },
#      ...
#    ]

# 3. Score your atlas against the gold standard
python -m evaluation.scorer \
    --atlas path/to/your_atlas.json \
    --gold gold_standard/parsed/phosphoatlas_gold.json \
    --output results/scores/your_model_name

# 4. Check your scores
cat results/scores/your_model_name/summary.json
```

### What to upload

Create a folder under `contributions/` with your name/model:

```
contributions/
└── your_name_model/
    ├── agent_runner.py        # Your agent implementation
    ├── atlas.json             # Raw atlas output from your agent
    └── scores/                # Output from the scorer
        ├── summary.json
        ├── per_kinase.json
        └── peptide_mismatches.json
```

Commit and push your folder. Do NOT modify files outside your own folder.

## Repository Structure

```
agent-pa-benchmark/
├── run_experiment.py              # Entry point (parse, score, compare)
│
├── gold_standard/                 # Gold standard data
│   ├── parsed/
│   │   ├── phosphoatlas_gold.json # Structured gold standard (use this for scoring)
│   │   └── phosphoatlas_gold.csv  # Flat CSV (for inspection)
│   ├── parse_pa2.py               # Parser (if you need to re-parse from XLSX)
│   └── sample_PA2.xlsx            # Format reference
│
├── agents/                        # Agent framework
│   ├── base_agent.py              # Abstract base class (tool loop, budget, logging)
│   └── prompts/
│       ├── naive.txt              # START HERE — zero-shot, no guidance
│       ├── paper_informed.txt     # Includes PA paper context
│       └── pipeline_guided.txt    # Includes S1 pipeline steps
│
├── evaluation/                    # Scoring pipeline
│   ├── scorer.py                  # Main scorer (run this)
│   ├── normalizer.py              # Gene symbol / phospho-site normalization
│   └── analyzer.py                # Cross-model comparison
│
├── contributions/                 # YOUR WORK GOES HERE
│   └── example/                   # Example structure
│
├── paper/                         # Manuscript assets
│   ├── figures/
│   ├── tables/
│   └── supplementary/
│
└── results/                       # Aggregated results
    └── summaries/
```

## Scoring Metrics

Your agent is evaluated on:

| Metric | What it measures |
|--------|-----------------|
| **Precision** | Fraction of agent entries that are in the gold standard |
| **Recall** | Fraction of gold standard entries the agent found |
| **F1** | Harmonic mean of precision and recall |
| **Kinase discovery** | How many of the 438 gold-standard kinases were found |
| **Peptide accuracy** | For matched entries, did the heptameric peptide match? |
| **UniProt accuracy** | For matched entries, did the substrate UniProt ID match? |
| **Per-tier recall** | Recall broken down by kinase size (A/B/C/D tiers) |

## Experimental Conditions

Start with `naive`. We may ask you to run additional conditions later.

| Condition | Prompt file | Description |
|-----------|-------------|-------------|
| `naive` | `agents/prompts/naive.txt` | Zero-shot: "build a phosphorylation atlas" + tools, no guidance |
| `paper_informed` | `agents/prompts/paper_informed.txt` | Agent receives PhosphoAtlas paper context |
| `pipeline_guided` | `agents/prompts/pipeline_guided.txt` | Agent receives explicit S1 pipeline steps |

## Atlas JSON Format

Your agent must produce a JSON array. Each entry must have at minimum:

```json
{
  "kinase_gene": "CDK1",
  "substrate_gene": "RB1",
  "phospho_site": "S807"
}
```

Optional but scored fields: `heptameric_peptide`, `substrate_uniprot`, `supporting_databases`.

## Questions?

Open an issue or contact the project maintainers.
