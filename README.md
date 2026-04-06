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
- **Data sources**: PSP, SIGNOR, UniProt — downloaded live from the web during each run

### 3. Run your agent

#### Recommended: Live Runner (no API key needed)

The live runner downloads data from real web sources (PhosphoSitePlus, SIGNOR, UniProt REST API), builds the atlas, scores it against the gold standard, and estimates token costs for your model. **This is the standard way to run experiments.**

```bash
# See available models and their pricing
python3 agents/live_runner.py --list-models

# Run a single condition
python3 agents/live_runner.py --model opus --condition naive
python3 agents/live_runner.py --model gemini-pro --condition paper_informed
python3 agents/live_runner.py --model qwen-235b --condition pipeline_guided

# Run ALL conditions (naive + paper_informed + pipeline_guided + iterative)
python3 agents/live_runner.py --model opus --all

# Custom output directory
python3 agents/live_runner.py --model sonnet --condition naive \
    --output-dir contributions/claude_sonnet_naive
```

> **Note**: When using `--all`, results are saved to `contributions/<model>_<condition>/`.
> Move them to your preferred folder names if needed (e.g. `claude_opus_naive`).

#### Alternative: Model-specific API runner (token-billed models)

For **Qwen** and **Mistral** (pay-per-token APIs), the model-specific runners capture **exact** token counts and costs from the API response:

```bash
# Qwen3-235B via Together AI
export TOGETHER_API_KEY=your-key
python3 contributions/andrew_qwen3_235b/agent_runner.py

# Mistral Large
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

For **Gemini**: use the runner in `contributions/gemini_3.1_pro_naive/gemini_agent.py` (requires `GOOGLE_API_KEY`).

#### Custom agent (any model/framework)

Write your own runner. It must:
1. Produce `atlas.json` — array of kinase-substrate-site entries
2. Save `run_log.json` with a `token_usage` field (see Section 5)
3. Run the scorer to generate `scores/summary.json`

### 4. Score your atlas

The live runner scores automatically. To score manually:

```bash
python3 -m evaluation.scorer \
    --atlas contributions/your_model/atlas.json \
    --gold gold_standard/parsed/phosphoatlas_gold.json \
    --output contributions/your_model/scores
```

### 5. What to upload

Create a folder under `contributions/` and commit:

```
contributions/
└── your_model_condition/
    ├── agent_runner.py        # Your agent implementation
    ├── atlas.json             # Atlas output
    ├── run_log.json           # MUST include token_usage (see below)
    └── scores/
        ├── summary.json
        ├── per_kinase.json
        └── peptide_mismatches.json
```

Do NOT commit database files, `.env`, or API keys. Do NOT modify files outside your folder.

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

| Model type | How to get token data |
|---|---|
| **Token-billed** (Qwen/Together, Mistral) | Extract exact counts from `response.usage.prompt_tokens` / `completion_tokens` |
| **Subscription** (Claude Max, Gemini, GPT) | Use `live_runner.py` for estimation, or track `usage_metadata` from API response |

### 6. Iterative runs (3 rounds with feedback)

The benchmark supports iterative refinement: 3 rounds where each round receives scoring feedback from the previous round(s). This measures whether an agent can improve its curation strategy when told what it missed.

```bash
# Iterative run for a single condition
python3 agents/live_runner.py --model opus --condition naive --iterative

# Run everything: all 3 conditions + 3-round iterative on naive
python3 agents/live_runner.py --model opus --all

# Iterative for other models
python3 agents/live_runner.py --model gemini-pro --condition naive --iterative
python3 agents/live_runner.py --model qwen-235b --condition paper_informed --iterative
```

Output structure for iterative runs:
```
contributions/claude_opus_naive/
├── atlas.json                  # Round 1 (baseline)
├── run_log.json
├── scores/
├── round2/                     # Round 2 (receives Round 1 feedback)
│   ├── atlas.json
│   ├── run_log.json
│   └── scores/
├── round3/                     # Round 3 (receives Round 1+2 feedback)
│   ├── atlas.json
│   ├── run_log.json
│   └── scores/
└── iterative_comparison.json   # Cross-round comparison
```

### 7. Regenerate the report

After adding your results, regenerate the comparison PDF:

```bash
python3 -m evaluation.report
# Output: paper/tables/benchmark_summary_tables.pdf
```

### 8. Database setup (optional, for local runner only)

The live runner (`live_runner.py`) downloads everything from the web — no local setup needed.

If you want to use the local runner (`local_runner.py`) or `DatabaseTools` directly, you need local database files:

```bash
# Place or symlink database files (not committed to git):
databases/
├── psp/Kinase_Substrate_Dataset       # From PhosphoSitePlus (requires license)
├── signor/signor_phospho_human.json   # From SIGNOR API
└── uniprot/uniprot_phospho_parsed.json # Parsed from UniProt REST API
```

Contact Hui Lin for access to the database files.

## Repository Structure

```
agent-pa-benchmark/
├── agents/                            # Agent framework
│   ├── base_agent.py                  # Abstract base class (tool loop, budget, token tracking)
│   ├── live_runner.py                 # ** PRIMARY RUNNER ** — live web downloads, scoring, cost estimation
│   ├── claude_runner.py               # Claude API runner (needs ANTHROPIC_API_KEY)
│   ├── local_runner.py                # Local DB runner (needs database files)
│   └── prompts/
│       ├── naive.txt                  # Zero-shot, no guidance
│       ├── paper_informed.txt         # PhosphoAtlas paper context + PSP URL
│       └── pipeline_guided.txt        # Explicit S1 pipeline steps
│
├── databases/                         # Database tool interface
│   ├── tools.py                       # Universal query API (DatabaseTools class)
│   └── (psp/, signor/, uniprot/)      # Local data files (gitignored, optional)
│
├── evaluation/                        # Scoring pipeline
│   ├── scorer.py                      # Main scorer
│   ├── normalizer.py                  # Gene symbol / phospho-site normalization
│   ├── analyzer.py                    # Cross-model comparison
│   └── report.py                      # PDF report generator
│
├── gold_standard/                     # Gold standard (PhosphoAtlas 2.0)
│   ├── parsed/phosphoatlas_gold.json  # 15,640 triplets, 433 kinases
│   └── parsed/phosphoatlas_gold.csv   # Flat CSV for inspection
│
├── contributions/                     # YOUR WORK GOES HERE
│   ├── claude_opus_naive/             # Example: Claude Opus naive run
│   ├── andrew_qwen3_235b/            # Example: Qwen3-235B runs
│   └── ...
│
├── experiments/                       # Experimental scripts
│   └── iterative_refinement.py        # Standalone iterative experiment
│
├── paper/                             # Manuscript assets
│   ├── tables/benchmark_summary_tables.pdf
│   ├── figures/
│   └── supplementary/
│
└── results/                           # Aggregated results
```

## Scoring Metrics

| Metric | What it measures |
|--------|-----------------|
| **Precision** | Fraction of agent entries that are in the gold standard |
| **Recall** | Fraction of gold standard entries the agent found |
| **F1** | Harmonic mean of precision and recall |
| **Kinase discovery** | How many of the 433 gold-standard kinases were found |
| **Peptide accuracy** | For matched entries, did the heptameric peptide match? (case-insensitive) |
| **UniProt accuracy** | For matched entries, did the substrate UniProt ID match? |
| **Per-tier recall** | Recall by kinase size: A (100+), B (20-99), C (5-19), D (<5 substrates) |
| **Multi-DB %** | Fraction of entries confirmed by 2+ databases |
| **Token cost** | Estimated or actual API cost in USD |

## Experimental Conditions

Run all conditions for your model. The live runner handles this with `--all`.

| Condition | Prompt | Description |
|-----------|--------|-------------|
| `naive` | `agents/prompts/naive.txt` | Zero-shot: "build a phosphorylation atlas" + tools only |
| `paper_informed` | `agents/prompts/paper_informed.txt` | PhosphoAtlas paper context + PSP download URL |
| `pipeline_guided` | `agents/prompts/pipeline_guided.txt` | Explicit Olow et al. S1 pipeline steps |
| `iterative` | naive + feedback | 3 rounds: each round receives scoring feedback from prior rounds |

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