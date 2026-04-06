# PhosphoAtlas Agent Benchmark

Benchmarking AI agents on their ability to curate a comprehensive human protein phosphorylation atlas, evaluated against PhosphoAtlas 2.0 as the gold standard.

## For Students: Getting Started

### 1. Clone and install

```bash
git clone https://github.com/OncoNLP/agent-pa-benchmark.git
cd agent-pa-benchmark
pip install -r requirements.txt
```

### 2. Understand the benchmark

- **Gold standard**: `gold_standard/parsed/phosphoatlas_gold.json` (15,640 triplets, 433 kinases)
- **Prompts**: `agents/prompts/` — three conditions (naive, paper_informed, pipeline_guided)
- **Scorer**: `evaluation/scorer.py` — scores your atlas output

### 3. Run your agent

Choose the approach that matches your model:

#### Option A: Live Runner (recommended, no API key needed)

Downloads data from real web sources (PSP, SIGNOR, UniProt), builds the atlas, scores it, and estimates token costs for any model. This is the standard way to run all three conditions.

```bash
# List available models and their pricing
python3 agents/live_runner.py --list-models

# Run all three conditions for your model
python3 agents/live_runner.py --model opus --condition naive
python3 agents/live_runner.py --model opus --condition paper_informed
python3 agents/live_runner.py --model opus --condition pipeline_guided

# Other models
python3 agents/live_runner.py --model gemini-pro --condition naive
python3 agents/live_runner.py --model gpt-5 --condition paper_informed
python3 agents/live_runner.py --model qwen-235b --condition naive

# Custom output directory
python3 agents/live_runner.py --model sonnet --condition naive \
    --output-dir contributions/claude_sonnet_naive_v2
```

#### Option B: Model-specific API runner (token-based models)

For **Qwen** and **Mistral** (token-billed), use the model-specific runners which capture **exact** token counts and costs from the API:

```bash
# Qwen3-235B via Together AI (exact tokens tracked)
export TOGETHER_API_KEY=your-key
python3 contributions/andrew_qwen3_235b/agent_runner.py

# Mistral Large (exact tokens tracked)
export MISTRAL_API_KEY=your-key
python3 contributions/mistral_large_naive/agent_runner.py
python3 contributions/mistral_large_paper_informed/agent_runner.py
```

For **Claude** (if you have an API key):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 agents/claude_runner.py --model opus --condition naive \
    --output contributions/claude_opus_naive
```

#### Option C: Custom agent (any model/framework)

Write your own agent. Your runner must:
1. Produce `atlas.json` (array of entries)
2. Save `run_log.json` with `token_usage` field
3. Run the scorer

```python
from databases.tools import DatabaseTools
tools = DatabaseTools("databases/")
tools.list_databases()
tools.query_by_kinase("psp", "CDK1")
```

### 4. Score your atlas

```bash
python -m evaluation.scorer \
    --atlas contributions/your_model/atlas.json \
    --gold gold_standard/parsed/phosphoatlas_gold.json \
    --output contributions/your_model/scores
```

### 5. What to upload

```
contributions/
└── your_model_condition/
    ├── agent_runner.py        # Your agent implementation
    ├── atlas.json             # Atlas output
    ├── run_log.json           # MUST include token_usage field (see below)
    └── scores/
        ├── summary.json
        ├── per_kinase.json
        └── peptide_mismatches.json
```

**Token tracking is required.** Your `run_log.json` must include:

```json
{
  "token_usage": {
    "total_input_tokens": 150000,
    "total_output_tokens": 5000,
    "total_tokens": 155000,
    "estimated_cost_usd": 2.50,
    "api_calls": 10
  }
}
```

- **Token-billed models** (Qwen/Together AI, Mistral): Extract exact counts from `response.usage`
- **Subscription models** (Claude Max, Gemini, GPT): Use `live_runner.py` for estimation, or track `usage_metadata` from the API response

### 6. Iterative runs (3 rounds with feedback)

The benchmark supports iterative refinement: run 3 rounds where each subsequent round receives performance feedback from the previous round(s). This measures whether the agent can improve its curation when told what it missed.

```bash
# Iterative run for a single condition
python3 agents/live_runner.py --model opus --condition naive --iterative

# Run everything: all 3 conditions + iterative on naive
python3 agents/live_runner.py --model opus --all

# Iterative for other models
python3 agents/live_runner.py --model gemini-pro --condition naive --iterative
python3 agents/live_runner.py --model qwen-235b --condition paper_informed --iterative
```

Output structure for iterative runs:
```
contributions/claude_opus_naive/
├── atlas.json           # Round 1 (baseline)
├── run_log.json
├── scores/
├── round2/              # Round 2 (with Round 1 feedback)
│   ├── atlas.json
│   ├── run_log.json
│   └── scores/
├── round3/              # Round 3 (with Round 1+2 feedback)
│   ├── atlas.json
│   ├── run_log.json
│   └── scores/
└── iterative_comparison.json  # Cross-round comparison
```

### 7. Regenerate the report

After adding your results, regenerate the comparison PDF:

```bash
python3 -m evaluation.report
# Output: paper/tables/benchmark_summary_tables.pdf
```

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

Contact Hui Lin for details.