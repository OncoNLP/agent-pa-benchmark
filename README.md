# PhosphoAtlas Agent Benchmark

Benchmarking AI agents on their ability to curate a comprehensive human protein phosphorylation atlas from public databases, evaluated against PhosphoAtlas 2.0 as the gold standard.

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/OncoNLP/agent-pa-benchmark.git
cd agent-pa-benchmark
pip install -r requirements.txt

# 2. Add database files to databases/ (contact maintainers for setup)

# 3. Parse gold standard
python run_experiment.py --step parse --pa2-input gold_standard/input/PA2.xlsx

# 4. Implement your agent runner (see agents/base_agent.py)

# 5. Score your atlas
python run_experiment.py --step score --atlas results/raw/your_atlas.json

# 6. Compare across models
python run_experiment.py --step compare
```

## Repository Structure

```
agent-pa-benchmark/
├── run_experiment.py          # Single entry point
├── config.yaml                # Model configs, conditions, budgets
│
├── gold_standard/             # PA2.0 gold standard
│   ├── parse_pa2.py           # XLSX → JSON parser
│   ├── sample_PA2.xlsx        # Format reference
│   └── input/                 # Place full PA2 XLSX here (gitignored)
│
├── databases/                 # Local DB files + query interface
│   ├── tools.py               # DatabaseTools — universal query API
│   └── */                     # Database subdirectories (gitignored)
│
├── agents/                    # Agent implementations
│   ├── base_agent.py          # Abstract base class (tool loop, logging, budget)
│   └── prompts/               # Experiment prompts
│       ├── naive.txt          # Zero-shot, no guidance
│       ├── paper_informed.txt # PA paper context provided
│       ├── pipeline_guided.txt# S1 pipeline steps provided
│       ├── knowledge_only.txt # No tools, pure LLM knowledge
│       └── multi_agent.txt    # Curation + QC + master
│
├── evaluation/                # Scoring pipeline
│   ├── scorer.py              # Triplet + column-level scoring
│   ├── normalizer.py          # Gene symbol / site normalization
│   └── analyzer.py            # Cross-model comparison
│
├── experiments/               # Experiment configs (YAML)
├── results/                   # Outputs (gitignored)
│   ├── raw/                   # Atlas JSONs
│   ├── scores/                # Score files
│   └── summaries/             # Comparison tables
│
├── paper/                     # Manuscript assets
│   ├── figures/
│   ├── tables/
│   └── supplementary/
│
└── scripts/                   # Utilities
    └── setup_server.sh        # Server setup
```

## Experimental Conditions

| Condition | Tools | Guidance | Tests |
|-----------|-------|----------|-------|
| `naive` | Yes | None | Zero-shot tool use strategy |
| `paper_informed` | Yes | Paper abstract + methods | Effect of domain context |
| `pipeline_guided` | Yes | S1 pipeline steps | Effect of explicit instructions |
| `knowledge_only` | No | None | Inherent biological knowledge |
| `iterative` | Yes | Feedback after each round | Self-improvement capability |
| `multi_agent` | Yes | Role assignments | Multi-agent coordination |

## Implementing an Agent Runner

Subclass `BaseAgent` and implement 4 methods:

```python
from agents.base_agent import BaseAgent

class MyAgent(BaseAgent):
    def _call_model(self, messages, tools):
        # Call your LLM API
        ...

    def _parse_tool_calls(self, response):
        # Extract [(tool_name, args), ...] from response
        ...

    def _parse_text(self, response):
        # Extract text content from response
        ...

    def _format_tool_result(self, tool_name, result):
        # Format tool result as a message for the model
        ...
```

Then run:

```python
agent = MyAgent(model_name="my-model", databases_dir="databases")
prompt = open("agents/prompts/naive.txt").read()
result = agent.run(prompt, condition="naive")

# Save atlas
import json
with open("results/raw/my-model_naive_run0.json", "w") as f:
    json.dump(result["atlas"], f)
```

## Scoring Metrics

- **Triplet-level**: Precision, Recall, F1 on (kinase, substrate, site) matching
- **Column-level**: Accuracy of phospho-site, heptameric peptide, UniProt ID
- **Kinase discovery**: Fraction of gold-standard kinases found
- **Cross-referencing**: Fraction of entries verified across multiple DBs
- **Per-tier**: Recall by kinase size (A: 100+, B: 20-99, C: 5-19, D: <5 substrates)

## Database Tools API

The agent interacts with databases via `DatabaseTools`:

| Tool | Description |
|------|-------------|
| `list_databases()` | Available databases |
| `get_stats(db)` | Entry/kinase/substrate counts |
| `list_kinases(db, offset, limit)` | Paginated kinase list |
| `list_substrates(db, offset, limit)` | Paginated substrate list |
| `search(db, keyword, limit)` | Free-text search |
| `query_by_kinase(db, gene)` | All substrates for a kinase |
| `query_by_substrate(db, gene)` | All kinases for a substrate |
| `query_by_site(db, gene, site)` | Specific gene+site records |
| `query_all_dbs(gene)` | Cross-database query |
| `submit_atlas(entries)` | Submit final atlas |

## Contributing

Each team member implements their assigned model's agent runner in `agents/` and commits results to `results/`.
