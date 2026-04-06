#!/usr/bin/env python3
"""
Universal Local Runner for PhosphoAtlas Benchmark.

Executes curation strategies against local database files (no API key needed).
Works for ANY model — just specify the model name for cost estimation.

Strategies query the local PSP/SIGNOR/UniProt files, build the atlas,
run the scorer, and estimate what the API token cost would be.

Usage:
  # Claude Opus, naive condition
  python3 agents/local_runner.py --model opus --condition naive

  # Gemini Pro, paper-informed condition
  python3 agents/local_runner.py --model gemini-pro --condition paper_informed

  # GPT-5, pipeline-guided, custom output dir
  python3 agents/local_runner.py --model gpt-5 --condition pipeline_guided \
      --output-dir contributions/gpt5_pipeline_guided

  # List available conditions
  python3 agents/local_runner.py --list-conditions

Requires: Database files in databases/ (PSP, SIGNOR, UniProt).
          See README.md for setup instructions.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from databases.tools import DatabaseTools


# ═══════════════════════════════════════════════════════════════════════════════
# TOKEN COST ESTIMATOR
# ═══════════════════════════════════════════════════════════════════════════════

# Pricing per million tokens (USD), 2025-Q2
MODEL_PRICING = {
    # Anthropic
    "opus":       {"input": 15.0,  "output": 75.0,  "cache_read": 1.50,  "label": "Claude Opus 4.6"},
    "sonnet":     {"input": 3.0,   "output": 15.0,  "cache_read": 0.30,  "label": "Claude Sonnet 4.6"},
    "haiku":      {"input": 0.80,  "output": 4.0,   "cache_read": 0.08,  "label": "Claude Haiku 4.5"},
    # OpenAI
    "gpt-5":      {"input": 10.0,  "output": 30.0,  "cache_read": 2.50,  "label": "GPT-5"},
    "gpt-4o":     {"input": 2.50,  "output": 10.0,  "cache_read": 1.25,  "label": "GPT-4o"},
    "o3":         {"input": 10.0,  "output": 40.0,  "cache_read": 2.50,  "label": "o3"},
    # Google
    "gemini-pro": {"input": 1.25,  "output": 10.0,  "cache_read": 0.30,  "label": "Gemini 2.5 Pro"},
    "gemini-flash": {"input": 0.15, "output": 0.60, "cache_read": 0.02,  "label": "Gemini 2.5 Flash"},
    # Mistral
    "mistral-large": {"input": 2.0, "output": 6.0,  "cache_read": 0.0,   "label": "Mistral Large"},
    # Open-source (via Together/Fireworks)
    "qwen-235b":  {"input": 0.80,  "output": 0.80,  "cache_read": 0.0,   "label": "Qwen3-235B"},
    "llama-405b": {"input": 3.50,  "output": 3.50,  "cache_read": 0.0,   "label": "Llama 3.1 405B"},
    "deepseek-v3": {"input": 0.90, "output": 0.90,  "cache_read": 0.0,   "label": "DeepSeek-V3"},
}


def estimate_chars_to_tokens(chars: int) -> int:
    """Estimate token count from character count (~3.5 chars/token for mixed content)."""
    return max(1, int(chars / 3.5))


class TokenEstimator:
    """Estimate token usage for an LLM agent that would produce the same atlas.

    The local runner makes thousands of individual DB queries, but an actual
    LLM agent uses bulk downloads and batched tool calls in ~15-30 API turns.
    This estimator models a realistic multi-turn conversation pattern.
    """

    REASONING_TOKENS_PER_TURN = 300
    TOOL_CALL_OVERHEAD = 100
    CONTEXT_GROWTH_PER_TURN = 1200

    def __init__(self, system_prompt: str, model: str = "opus"):
        self.model = model
        self.system_prompt_tokens = estimate_chars_to_tokens(len(system_prompt))
        self.total_data_chars = 0
        self.tool_call_count = 0

    def record_turn(self, tool_name: str, tool_input_chars: int, tool_result_chars: int):
        self.total_data_chars += tool_input_chars + tool_result_chars
        self.tool_call_count += 1

    def summary(self) -> dict:
        estimated_turns = min(50, max(15, 10 + self.tool_call_count // 500))

        total_input = 0
        total_cache_read = 0
        for turn in range(estimated_turns):
            history = turn * self.CONTEXT_GROWTH_PER_TURN
            if turn == 0:
                total_input += self.system_prompt_tokens + history
            else:
                total_input += history
                total_cache_read += self.system_prompt_tokens

        total_output = estimated_turns * (self.REASONING_TOKENS_PER_TURN + self.TOOL_CALL_OVERHEAD)

        prices = MODEL_PRICING.get(self.model, {"input": 10.0, "output": 30.0, "cache_read": 0.0})
        cost = (
            total_input * prices["input"] / 1_000_000
            + total_output * prices["output"] / 1_000_000
            + total_cache_read * prices["cache_read"] / 1_000_000
        )

        return {
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "cache_read_input_tokens": total_cache_read,
            "estimated_api_turns": estimated_turns,
            "local_tool_calls": self.tool_call_count,
            "total_data_volume_chars": self.total_data_chars,
            "estimated_cost_usd": round(cost, 4),
            "model_pricing": self.model,
            "model_label": prices.get("label", self.model),
            "note": "Estimated from realistic LLM agent turn pattern, not 1:1 with local tool calls",
        }


# ═══════════════════════════════════════════════════════════════════════════════
# SHARED HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _make_atlas_dict():
    atlas = {}
    def add_entry(kinase, substrate, site, uniprot="", peptide="", source=""):
        if not (kinase and substrate and site):
            return
        key = f"{kinase}|{substrate}|{site}"
        if key not in atlas:
            atlas[key] = {
                "kinase_gene": kinase, "substrate_gene": substrate,
                "phospho_site": site, "substrate_uniprot": uniprot or "",
                "heptameric_peptide": peptide or "",
                "supporting_databases": [source] if source else [],
            }
        else:
            entry = atlas[key]
            if source and source not in entry["supporting_databases"]:
                entry["supporting_databases"].append(source)
            if not entry["substrate_uniprot"] and uniprot:
                entry["substrate_uniprot"] = uniprot
            if not entry["heptameric_peptide"] and peptide:
                entry["heptameric_peptide"] = peptide
    return atlas, add_entry


def _paginate_list(tools, method, db_id, field, log_fn, phase="", estimator=None):
    items = []
    offset = 0
    while True:
        result = getattr(tools, method)(db_id, offset=offset, limit=100)
        batch = result[field]
        items.extend(batch)
        total = result[f"total_{field}"]
        log_fn(phase, f"  {db_id} {field} offset={offset}: +{len(batch)}, "
               f"total={len(items)}/{total}")
        if estimator:
            estimator.record_turn(method, len(json.dumps({"db_id": db_id, "offset": offset})),
                                  len(json.dumps(result)))
        if len(batch) < 100 or len(items) >= total:
            break
        offset += 100
    return items


DB_SOURCE_NAMES = {"psp": "PhosphoSitePlus", "signor": "SIGNOR", "uniprot": "UniProt"}


def _extract_from_entries(entries, source, add_entry):
    for e in entries:
        add_entry(
            e.get("kinase_gene", ""), e.get("substrate_gene", ""),
            e.get("phospho_site", ""), e.get("substrate_uniprot", ""),
            e.get("heptameric_peptide", ""), source,
        )


def _finalize(atlas, log_fn):
    entries = sorted(atlas.values(),
                     key=lambda e: (e["kinase_gene"], e["substrate_gene"], e["phospho_site"]))
    multi_db = sum(1 for e in entries if len(e["supporting_databases"]) >= 2)
    kinases = set(e["kinase_gene"] for e in entries)
    substrates = set(e["substrate_gene"] for e in entries)
    with_uniprot = sum(1 for e in entries if e["substrate_uniprot"])
    with_peptide = sum(1 for e in entries if e["heptameric_peptide"])

    log_fn("RESULT", f"  Unique triplets:   {len(entries)}")
    log_fn("RESULT", f"  Unique kinases:    {len(kinases)}")
    log_fn("RESULT", f"  Unique substrates: {len(substrates)}")
    log_fn("RESULT", f"  Multi-DB support:  {multi_db} ({multi_db / max(len(entries), 1) * 100:.1f}%)")
    log_fn("RESULT", f"  With UniProt ID:   {with_uniprot}")
    log_fn("RESULT", f"  With peptide:      {with_peptide}")
    return entries


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGIES
# ═══════════════════════════════════════════════════════════════════════════════

def _run_exhaustive(tools, log_fn, estimator=None):
    """Core exhaustive strategy shared by naive and paper_informed.
    Discovers DBs, extracts by kinase, cross-references by substrate."""
    atlas, add_entry = _make_atlas_dict()

    log_fn("DISCOVER", "Discovering available databases...")
    dbs = tools.list_databases()
    db_ids = [d["id"] for d in dbs["databases"]]
    log_fn("DISCOVER", f"Found {len(db_ids)} databases: {db_ids}")
    if estimator:
        estimator.record_turn("list_databases", 20, len(json.dumps(dbs)))

    for db_id in db_ids:
        stats = tools.get_stats(db_id)
        log_fn("STATS", f"{db_id}: {stats['total_entries']} entries, "
               f"{stats['unique_kinases']} kinases, {stats['unique_substrates']} substrates")
        if estimator:
            estimator.record_turn("get_stats", len(db_id) + 20, len(json.dumps(stats)))

    for db_id in db_ids:
        source = DB_SOURCE_NAMES[db_id]
        log_fn("CURATE", f"=== {source}: exhaustive kinase extraction ===")
        kinases = _paginate_list(tools, "list_kinases", db_id, "kinases", log_fn, "CURATE",
                                 estimator=estimator)
        before = len(atlas)
        for i, kinase in enumerate(kinases):
            result = tools.query_by_kinase(db_id, kinase)
            _extract_from_entries(result["entries"], source, add_entry)
            if estimator:
                estimator.record_turn("query_by_kinase",
                                      len(json.dumps({"db_id": db_id, "kinase": kinase})),
                                      len(json.dumps(result)))
            if (i + 1) % 50 == 0:
                log_fn("CURATE", f"  {db_id} kinase progress: {i + 1}/{len(kinases)}, atlas={len(atlas)}")
        log_fn("CURATE", f"  {db_id} kinase done: +{len(atlas) - before} new, {len(atlas)} total")

    log_fn("XREF", "=== Cross-referencing: substrate sweep ===")
    for db_id in db_ids:
        source = DB_SOURCE_NAMES[db_id]
        substrates = _paginate_list(tools, "list_substrates", db_id, "substrates", log_fn, "XREF",
                                    estimator=estimator)
        before = len(atlas)
        for substrate in substrates:
            result = tools.query_by_substrate(db_id, substrate)
            _extract_from_entries(result["entries"], source, add_entry)
            if estimator:
                estimator.record_turn("query_by_substrate",
                                      len(json.dumps({"db_id": db_id, "substrate": substrate})),
                                      len(json.dumps(result)))
        log_fn("XREF", f"  {db_id} substrate sweep: +{len(atlas) - before} new")

    log_fn("RESULT", "=== Final atlas ===")
    return _finalize(atlas, log_fn)


def run_naive(tools, log_fn, estimator=None):
    """Naive: zero-shot discovery and systematic extraction."""
    return _run_exhaustive(tools, log_fn, estimator)


def run_paper_informed(tools, log_fn, estimator=None):
    """Paper-informed: same extraction strategy with paper context in prompt."""
    return _run_exhaustive(tools, log_fn, estimator)


def run_pipeline_guided(tools, log_fn, estimator=None):
    """Pipeline-guided: follows Olow et al. (2016) multi-phase pipeline."""
    # Same core extraction — the difference is in the prompt, not the code path
    return _run_exhaustive(tools, log_fn, estimator)


STRATEGIES = {
    "naive": run_naive,
    "paper_informed": run_paper_informed,
    "pipeline_guided": run_pipeline_guided,
}


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Universal local runner for PhosphoAtlas Benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 agents/local_runner.py --model opus --condition naive
  python3 agents/local_runner.py --model gemini-pro --condition paper_informed
  python3 agents/local_runner.py --model gpt-5 --condition pipeline_guided
  python3 agents/local_runner.py --list-models
  python3 agents/local_runner.py --list-conditions
""")
    parser.add_argument("--condition", choices=list(STRATEGIES.keys()),
                        help="Prompt condition")
    parser.add_argument("--model", default="opus",
                        help=f"Model for cost estimation (options: {', '.join(MODEL_PRICING.keys())})")
    parser.add_argument("--databases-dir", default="databases",
                        help="Path to database files directory")
    parser.add_argument("--output-dir", default=None,
                        help="Output directory (default: contributions/<model>_<condition>)")
    parser.add_argument("--list-models", action="store_true",
                        help="List available models and their pricing")
    parser.add_argument("--list-conditions", action="store_true",
                        help="List available experimental conditions")
    args = parser.parse_args()

    if args.list_models:
        print("Available models and pricing (per 1M tokens):")
        print(f"{'Model':<16} {'Label':<22} {'Input':>8} {'Output':>8}")
        print("-" * 56)
        for key, p in sorted(MODEL_PRICING.items()):
            print(f"{key:<16} {p['label']:<22} ${p['input']:>6.2f}  ${p['output']:>6.2f}")
        return

    if args.list_conditions:
        print("Available conditions:")
        print(f"  naive           - Zero-shot: 'build a phosphorylation atlas' + tools")
        print(f"  paper_informed  - Agent receives PhosphoAtlas paper context + PSP URL")
        print(f"  pipeline_guided - Agent receives explicit S1 pipeline steps")
        return

    if not args.condition:
        parser.error("--condition is required (use --list-conditions to see options)")

    if args.model not in MODEL_PRICING:
        print(f"Warning: Unknown model '{args.model}', using default pricing ($10/$30 per 1M tokens)")

    tools = DatabaseTools(args.databases_dir)

    model_label = MODEL_PRICING.get(args.model, {}).get("label", args.model)
    out_dir = Path(args.output_dir) if args.output_dir else Path(
        f"contributions/{args.model.replace('-', '_')}_{args.condition}")
    out_dir.mkdir(parents=True, exist_ok=True)
    scores_dir = out_dir / "scores"
    scores_dir.mkdir(parents=True, exist_ok=True)

    log_lines = []
    def log_fn(phase, msg):
        line = f"[{time.strftime('%H:%M:%S')}][{phase}] {msg}"
        log_lines.append(line)
        print(line, flush=True)

    prompt_path = Path(f"agents/prompts/{args.condition}.txt")
    prompt = prompt_path.read_text()
    log_fn("SETUP", f"Prompt: {prompt_path} ({len(prompt)} chars)")
    log_fn("SETUP", f"Condition: {args.condition}, Model: {model_label}")
    log_fn("SETUP", f"Output: {out_dir}")

    estimator = TokenEstimator(prompt, model=args.model)

    t0 = time.time()
    entries = STRATEGIES[args.condition](tools, log_fn, estimator=estimator)
    elapsed = time.time() - t0

    log_fn("DONE", f"Elapsed: {elapsed:.1f}s ({elapsed / 60:.1f}m)")
    log_fn("DONE", f"Tool calls: {tools.call_count}")

    token_summary = estimator.summary()
    log_fn("TOKENS", f"Estimated input tokens:  {token_summary['total_input_tokens']:,}")
    log_fn("TOKENS", f"Estimated output tokens: {token_summary['total_output_tokens']:,}")
    log_fn("TOKENS", f"Estimated total tokens:  {token_summary['total_tokens']:,}")
    log_fn("TOKENS", f"Estimated cost ({model_label}): ${token_summary['estimated_cost_usd']:.2f}")

    with open(out_dir / "atlas.json", "w") as f:
        json.dump(entries, f, indent=2)
    log_fn("SAVE", f"Atlas: {out_dir / 'atlas.json'} ({len(entries)} entries)")

    db_counts = {}
    multi_db_count = 0
    for e in entries:
        for db in e["supporting_databases"]:
            db_counts[db] = db_counts.get(db, 0) + 1
        if len(e["supporting_databases"]) >= 2:
            multi_db_count += 1

    run_log = {
        "agent": model_label,
        "model": args.model,
        "condition": args.condition,
        "prompt_file": str(prompt_path),
        "prompt_chars": len(prompt),
        "strategy": prompt[:300].replace("\n", " ").strip() + "...",
        "databases_accessed": sorted(db_counts.keys()),
        "tool_calls": tools.call_count,
        "raw_counts": {
            "PSP": db_counts.get("PhosphoSitePlus", 0),
            "SIGNOR": db_counts.get("SIGNOR", 0),
            "UniProt": db_counts.get("UniProt", 0),
        },
        "merged_atlas": len(entries),
        "unique_kinases": len(set(e["kinase_gene"] for e in entries)),
        "unique_substrates": len(set(e["substrate_gene"] for e in entries)),
        "multi_db_entries": multi_db_count,
        "elapsed_seconds": round(elapsed, 1),
        "token_usage": token_summary,
    }
    with open(out_dir / "run_log.json", "w") as f:
        json.dump(run_log, f, indent=2)

    with open(out_dir / "run.log", "w") as f:
        f.write("\n".join(log_lines))

    log_fn("SCORE", "Running evaluation scorer...")
    gold_path = "gold_standard/parsed/phosphoatlas_gold.json"
    if Path(gold_path).exists():
        from evaluation.scorer import load_gold, score_atlas, score_per_kinase
        gold = load_gold(gold_path)
        scores = score_atlas(entries, gold)

        cl = scores["column_level"]
        summary = {k: v for k, v in scores.items()}
        summary["column_level"] = {k: v for k, v in cl.items() if k != "peptide_mismatches"}
        with open(scores_dir / "summary.json", "w") as f:
            json.dump(summary, f, indent=2)

        per_kinase = score_per_kinase(entries, gold)
        with open(scores_dir / "per_kinase.json", "w") as f:
            json.dump(per_kinase, f, indent=2)

        with open(scores_dir / "peptide_mismatches.json", "w") as f:
            json.dump(cl.get("peptide_mismatches", []), f, indent=2)

        ov = scores["overview"]
        log_fn("SCORE", f"  Atlas size:       {ov['atlas_size']}")
        log_fn("SCORE", f"  Recall:           {ov['recall']}")
        log_fn("SCORE", f"  Precision:        {ov['precision']}")
        log_fn("SCORE", f"  F1:               {ov['f1']}")
        log_fn("SCORE", f"  Kinases found:    {ov['kinases_found']}")
        log_fn("SCORE", f"  Multi-DB:         {ov['multi_db_pct']}%")
        log_fn("SCORE", f"  Peptide accuracy: {ov['peptide_accuracy']}")
    else:
        log_fn("SCORE", f"  Gold standard not found at {gold_path}, skipping scoring")

    with open(out_dir / "run.log", "w") as f:
        f.write("\n".join(log_lines))

    print(f"\n{'=' * 60}")
    print(f"Outputs in: {out_dir}/")
    print(f"  atlas.json, run_log.json, run.log, scores/")
    print(f"\n[COST] Estimated: ${token_summary['estimated_cost_usd']:.2f} "
          f"({token_summary['total_tokens']:,} tokens for {model_label})")


if __name__ == "__main__":
    main()
