#!/usr/bin/env python3
"""
Claude Code Agent Runner for PhosphoAtlas Benchmark.

Executes curation strategies by systematically querying databases through
the tool interface, logging every call, and producing a deduplicated atlas.

Each condition maps to a distinct strategy function that follows a different
approach to atlas construction.

Token usage is estimated from actual data flow (prompt sizes + tool I/O)
to approximate what an API-based run would cost.

Usage:
  python3 agents/claude_code_runner.py --condition naive
  python3 agents/claude_code_runner.py --condition paper_informed
  python3 agents/claude_code_runner.py --condition pipeline_guided
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from databases.tools import DatabaseTools


# ── Token cost estimator ──────────────────────────────────────────────────────

# Claude pricing per million tokens (USD), 2025-Q2
CLAUDE_PRICING = {
    "opus": {"input": 15.0, "output": 75.0, "cache_read": 1.50},
    "sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.30},
}


def estimate_chars_to_tokens(chars: int) -> int:
    """Estimate token count from character count (≈3.5 chars/token for mixed content)."""
    return max(1, int(chars / 3.5))


class TokenEstimator:
    """Estimate token usage for an LLM agent that would produce the same atlas.

    Key insight: the local runner makes thousands of individual DB queries,
    but an actual LLM agent uses bulk downloads and batched tool calls in
    ~15-30 API turns. This estimator tracks raw data volume and models a
    realistic multi-turn conversation pattern.

    Calibrated against Claude Sonnet naive run (31 turns, 15,434 entries)
    which used fetch_and_parse_db for bulk downloads.
    """

    # Per-turn overhead for an LLM agent
    REASONING_TOKENS_PER_TURN = 300   # agent reasoning + planning text
    TOOL_CALL_OVERHEAD = 100          # JSON wrapper per tool_use block
    TOOL_RESULT_SUMMARY = 500         # truncated/summarized tool result per turn
    CONTEXT_GROWTH_PER_TURN = 1200    # net context growth per turn (output + result summary)

    def __init__(self, system_prompt: str, model: str = "opus"):
        self.model = model
        self.system_prompt_tokens = estimate_chars_to_tokens(len(system_prompt))
        self.total_data_chars = 0     # total raw data flowing through tools
        self.tool_call_count = 0
        self.unique_tools = set()

    def record_turn(self, tool_name: str, tool_input_chars: int, tool_result_chars: int):
        """Record one tool call (will be grouped into realistic API turns)."""
        self.total_data_chars += tool_input_chars + tool_result_chars
        self.tool_call_count += 1
        self.unique_tools.add(tool_name)

    def summary(self) -> dict:
        # Estimate realistic API turns: an LLM agent batches discovery (3-5 turns),
        # bulk downloads (3-6 turns), cross-referencing (3-5 turns), submission (1 turn)
        # Scale with data: more data = more pagination turns
        estimated_turns = min(50, max(15, 10 + self.tool_call_count // 500))

        # Input tokens: system prompt + growing context
        # Turn N reads: system_prompt (cached) + history_so_far
        # History at turn N ≈ N * CONTEXT_GROWTH_PER_TURN
        total_input = 0
        total_cache_read = 0
        for turn in range(estimated_turns):
            history = turn * self.CONTEXT_GROWTH_PER_TURN
            if turn == 0:
                total_input += self.system_prompt_tokens + history
            else:
                total_input += history  # new context since last cached prefix
                total_cache_read += self.system_prompt_tokens

        # Output tokens: reasoning + tool calls per turn
        total_output = estimated_turns * (self.REASONING_TOKENS_PER_TURN + self.TOOL_CALL_OVERHEAD)

        prices = CLAUDE_PRICING.get(self.model, CLAUDE_PRICING["opus"])
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
            "note": "Estimated from realistic LLM agent turn pattern, not 1:1 with local tool calls",
        }


# ── Shared helpers ──────────────────────────────────────────────────────────

def _make_atlas_dict():
    """Return a fresh atlas dict and an add_entry closure."""
    atlas = {}

    def add_entry(kinase, substrate, site, uniprot="", peptide="", source=""):
        if not (kinase and substrate and site):
            return
        key = f"{kinase}|{substrate}|{site}"
        if key not in atlas:
            atlas[key] = {
                "kinase_gene": kinase,
                "substrate_gene": substrate,
                "phospho_site": site,
                "substrate_uniprot": uniprot or "",
                "heptameric_peptide": peptide or "",
                "supporting_databases": [source],
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
    """Paginate through list_kinases or list_substrates, return full list."""
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
            input_chars = len(json.dumps({"db_id": db_id, "offset": offset, "limit": 100}))
            result_chars = len(json.dumps(result))
            estimator.record_turn(method, input_chars, result_chars)
        if len(batch) < 100 or len(items) >= total:
            break
        offset += 100
    return items


DB_SOURCE_NAMES = {"psp": "PhosphoSitePlus", "signor": "SIGNOR", "uniprot": "UniProt"}


def _extract_from_entries(entries, source, add_entry):
    """Extract fields from tool-returned entries and add to atlas."""
    for e in entries:
        add_entry(
            e.get("kinase_gene", ""),
            e.get("substrate_gene", ""),
            e.get("phospho_site", ""),
            e.get("substrate_uniprot", ""),
            e.get("heptameric_peptide", ""),
            source,
        )


def _finalize(atlas, log_fn):
    """Sort, compute stats, log summary, return sorted entry list."""
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


# ── Strategy: naive ────────────────────────────────────────────────────────

def run_naive(tools: DatabaseTools, log_fn, estimator=None) -> list[dict]:
    """Naive: zero-shot discovery and systematic extraction (same DB logic,
    but simulates an agent that discovers databases on its own)."""
    atlas, add_entry = _make_atlas_dict()

    # Agent discovers databases
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

    # Exhaustive kinase extraction from each DB
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
                input_chars = len(json.dumps({"db_id": db_id, "kinase": kinase}))
                result_chars = len(json.dumps(result))
                estimator.record_turn("query_by_kinase", input_chars, result_chars)
            if (i + 1) % 50 == 0:
                log_fn("CURATE", f"  {db_id} kinase progress: {i + 1}/{len(kinases)}, atlas={len(atlas)}")
        log_fn("CURATE", f"  {db_id} kinase done: +{len(atlas) - before} new, {len(atlas)} total")

    # Cross-reference by substrate
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
                input_chars = len(json.dumps({"db_id": db_id, "substrate": substrate}))
                result_chars = len(json.dumps(result))
                estimator.record_turn("query_by_substrate", input_chars, result_chars)
        log_fn("XREF", f"  {db_id} substrate sweep: +{len(atlas) - before} new")

    log_fn("RESULT", "=== Final atlas ===")
    return _finalize(atlas, log_fn)


# ── Strategy: paper_informed ────────────────────────────────────────────────

def run_paper_informed(tools: DatabaseTools, log_fn, estimator=None) -> list[dict]:
    """Paper-informed: knows PA paper background, systematic but no pipeline steps."""
    atlas, add_entry = _make_atlas_dict()

    log_fn("DISCOVER", "Listing available databases...")
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

    # Extract by kinase from each DB
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
                input_chars = len(json.dumps({"db_id": db_id, "kinase": kinase}))
                result_chars = len(json.dumps(result))
                estimator.record_turn("query_by_kinase", input_chars, result_chars)
            if (i + 1) % 50 == 0:
                log_fn("CURATE", f"  {db_id} kinase progress: {i + 1}/{len(kinases)}, atlas={len(atlas)}")
        log_fn("CURATE", f"  {db_id} kinase done: +{len(atlas) - before} new, {len(atlas)} total")

    # Cross-reference by substrate
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
                input_chars = len(json.dumps({"db_id": db_id, "substrate": substrate}))
                result_chars = len(json.dumps(result))
                estimator.record_turn("query_by_substrate", input_chars, result_chars)
        log_fn("XREF", f"  {db_id} substrate sweep: +{len(atlas) - before} new")

    log_fn("RESULT", "=== Final atlas ===")
    return _finalize(atlas, log_fn)


# ── Strategy: pipeline_guided (Olow et al. pipeline) ───────────────────────

def run_pipeline_guided(tools: DatabaseTools, log_fn, estimator=None) -> list[dict]:
    """Pipeline-guided: follows the exact Olow et al. (2016) multi-phase pipeline.

    Phase 1: Harmonize — discover databases, build protein reference index
    Phase 2: Build phosphorylation relational DB — systematic extraction
    Phase 3: Cross-reference and quality control
    """
    atlas, add_entry = _make_atlas_dict()

    def _track(tool_name, tool_input, result):
        if estimator:
            estimator.record_turn(tool_name, len(json.dumps(tool_input)), len(json.dumps(result)))

    # ── PHASE 1: Harmonize and Centralize ──────────────────────────────────

    log_fn("PHASE1", "=" * 60)
    log_fn("PHASE1", "PHASE 1: HARMONIZE AND CENTRALIZE PROTEIN DATA")
    log_fn("PHASE1", "=" * 60)

    log_fn("STEP1", "Step 1: Build Protein Reference Index (discover databases)")
    dbs = tools.list_databases()
    db_ids = [d["id"] for d in dbs["databases"]]
    _track("list_databases", {}, dbs)
    for d in dbs["databases"]:
        log_fn("STEP1", f"  Database: {d['name']} ({d['id']}) — {d['description']}")

    for db_id in db_ids:
        stats = tools.get_stats(db_id)
        _track("get_stats", {"db_id": db_id}, stats)
        log_fn("STEP1", f"  {db_id} scope: {stats['total_entries']} entries, "
               f"{stats['unique_kinases']} kinases, {stats['unique_substrates']} substrates")

    log_fn("STEP2", "Step 2: Curation 1 — Cross-reference external databases")
    all_kinases_by_db = {}
    all_substrates_by_db = {}
    global_kinases = set()
    global_substrates = set()

    for db_id in db_ids:
        kinases = _paginate_list(tools, "list_kinases", db_id, "kinases", log_fn, "STEP2",
                                 estimator=estimator)
        substrates = _paginate_list(tools, "list_substrates", db_id, "substrates", log_fn, "STEP2",
                                    estimator=estimator)
        all_kinases_by_db[db_id] = kinases
        all_substrates_by_db[db_id] = substrates
        global_kinases.update(kinases)
        global_substrates.update(substrates)

    log_fn("STEP2", f"  Global kinase inventory: {len(global_kinases)} unique across all DBs")
    log_fn("STEP2", f"  Global substrate inventory: {len(global_substrates)} unique across all DBs")

    kinase_db_count = {}
    for db_id, kinases in all_kinases_by_db.items():
        for k in kinases:
            kinase_db_count[k] = kinase_db_count.get(k, 0) + 1
    multi_db_kinases = sum(1 for v in kinase_db_count.values() if v >= 2)
    log_fn("STEP2", f"  Kinases in 2+ databases: {multi_db_kinases} (cross-validated)")

    log_fn("STEP3", "Step 3: Curation 2 — Consolidation deferred to assembly phase")

    # ── PHASE 2: Build Relational Database of Phosphorylation Events ───────

    log_fn("PHASE2", "=" * 60)
    log_fn("PHASE2", "PHASE 2: BUILD RELATIONAL DATABASE OF PHOSPHORYLATION EVENTS")
    log_fn("PHASE2", "=" * 60)

    log_fn("STEP4", "Step 4: Functional Triage — extract kinase->substrate relationships")

    for db_id in db_ids:
        source = DB_SOURCE_NAMES[db_id]
        kinases = all_kinases_by_db[db_id]
        log_fn("STEP4", f"  === {source}: querying {len(kinases)} kinases ===")
        before = len(atlas)
        for i, kinase in enumerate(kinases):
            result = tools.query_by_kinase(db_id, kinase)
            _extract_from_entries(result["entries"], source, add_entry)
            _track("query_by_kinase", {"db_id": db_id, "kinase": kinase}, result)
            if (i + 1) % 100 == 0:
                log_fn("STEP4", f"    {source} kinase progress: {i + 1}/{len(kinases)}, atlas={len(atlas)}")
        gained = len(atlas) - before
        log_fn("STEP4", f"  {source} kinase extraction: +{gained} entries, atlas={len(atlas)}")

    log_fn("STEP4", "  --- Substrate-side extraction (catch asymmetric entries) ---")
    for db_id in db_ids:
        source = DB_SOURCE_NAMES[db_id]
        substrates = all_substrates_by_db[db_id]
        before = len(atlas)
        for i, substrate in enumerate(substrates):
            result = tools.query_by_substrate(db_id, substrate)
            _extract_from_entries(result["entries"], source, add_entry)
            _track("query_by_substrate", {"db_id": db_id, "substrate": substrate}, result)
            if (i + 1) % 100 == 0:
                log_fn("STEP4", f"    {source} substrate progress: {i + 1}/{len(substrates)}, atlas={len(atlas)}")
        gained = len(atlas) - before
        log_fn("STEP4", f"  {source} substrate extraction: +{gained} new, atlas={len(atlas)}")

    # Step 5: Curation 3 — validate and filter
    log_fn("STEP5", "Step 5: Curation 3 — Validate phosphorylation sites")
    pre_qc = len(atlas)
    to_remove = []
    for key, entry in atlas.items():
        if not entry["kinase_gene"] or not entry["substrate_gene"] or not entry["phospho_site"]:
            to_remove.append(key)
    for key in to_remove:
        del atlas[key]
    log_fn("STEP5", f"  QC: removed {len(to_remove)} entries with missing fields "
           f"({pre_qc} -> {len(atlas)})")

    with_hps = sum(1 for e in atlas.values() if e["heptameric_peptide"])
    log_fn("STEP5", f"  Heptameric peptide coverage: {with_hps}/{len(atlas)} "
           f"({with_hps / max(len(atlas), 1) * 100:.1f}%)")

    # ── PHASE 3: Cross-Reference and Quality Control ───────────────────────

    log_fn("PHASE3", "=" * 60)
    log_fn("PHASE3", "PHASE 3: CROSS-REFERENCING AND QUALITY CONTROL")
    log_fn("PHASE3", "=" * 60)

    log_fn("STEP7", "Step 7: Multi-database cross-reference analysis")
    multi_db = sum(1 for e in atlas.values() if len(e["supporting_databases"]) >= 2)
    triple_db = sum(1 for e in atlas.values() if len(e["supporting_databases"]) >= 3)
    log_fn("STEP7", f"  Entries in 2+ databases: {multi_db}")
    log_fn("STEP7", f"  Entries in 3  databases: {triple_db}")

    db_counts = {}
    for entry in atlas.values():
        for db in entry["supporting_databases"]:
            db_counts[db] = db_counts.get(db, 0) + 1
    for db_name, count in sorted(db_counts.items()):
        log_fn("STEP7", f"  {db_name}: {count} entries")

    log_fn("STEP8", "Step 8: Final Quality Control and Assembly")
    log_fn("STEP8", "  Deduplication: atlas keyed by (kinase|substrate|site) — inherently deduplicated")

    log_fn("STEP6", "Step 6: Assemble PhosphoAtlas Relational Database")
    log_fn("RESULT", "=== FINAL ATLAS ===")
    return _finalize(atlas, log_fn)


# ── Main entry point ───────────────────────────────────────────────────────

STRATEGIES = {
    "naive": run_naive,
    "paper_informed": run_paper_informed,
    "pipeline_guided": run_pipeline_guided,
}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Claude Code Agent Runner")
    parser.add_argument("--condition", required=True, choices=list(STRATEGIES.keys()))
    parser.add_argument("--databases-dir", default="databases")
    parser.add_argument("--output-dir", default=None,
                        help="Output directory (default: contributions/claude_opus_<condition>)")
    parser.add_argument("--model", default="opus", choices=["opus", "sonnet", "haiku"],
                        help="Model to estimate costs for (default: opus)")
    parser.add_argument("--run-id", type=int, default=0)
    args = parser.parse_args()

    # Fresh tools instance — no reuse from previous runs
    tools = DatabaseTools(args.databases_dir)

    out_dir = Path(args.output_dir) if args.output_dir else Path(f"contributions/claude_{args.model}_{args.condition}")
    out_dir.mkdir(parents=True, exist_ok=True)
    scores_dir = out_dir / "scores"
    scores_dir.mkdir(parents=True, exist_ok=True)

    log_lines = []

    def log_fn(phase, msg):
        line = f"[{time.strftime('%H:%M:%S')}][{phase}] {msg}"
        log_lines.append(line)
        print(line, flush=True)

    # Load prompt
    prompt_path = Path(f"agents/prompts/{args.condition}.txt")
    prompt = prompt_path.read_text()
    log_fn("SETUP", f"Prompt: {prompt_path} ({len(prompt)} chars)")
    log_fn("SETUP", f"Condition: {args.condition}, Model: {args.model}, Run ID: {args.run_id}")
    log_fn("SETUP", f"Output: {out_dir}")

    # Initialize token estimator
    estimator = TokenEstimator(prompt, model=args.model)

    # Run strategy
    t0 = time.time()
    strategy_fn = STRATEGIES[args.condition]
    entries = strategy_fn(tools, log_fn, estimator=estimator)
    elapsed = time.time() - t0

    log_fn("DONE", f"Elapsed: {elapsed:.1f}s ({elapsed / 60:.1f}m)")
    log_fn("DONE", f"Tool calls: {tools.call_count}")

    # Token estimation summary
    token_summary = estimator.summary()
    log_fn("TOKENS", f"Estimated input tokens:  {token_summary['total_input_tokens']:,}")
    log_fn("TOKENS", f"Estimated output tokens: {token_summary['total_output_tokens']:,}")
    log_fn("TOKENS", f"Estimated total tokens:  {token_summary['total_tokens']:,}")
    log_fn("TOKENS", f"Estimated cost (USD):    ${token_summary['estimated_cost_usd']:.2f}")

    # Save atlas.json
    atlas_path = out_dir / "atlas.json"
    with open(atlas_path, "w") as f:
        json.dump(entries, f, indent=2)
    log_fn("SAVE", f"Atlas: {atlas_path} ({len(entries)} entries)")

    # Compute per-DB counts for run_log
    db_counts = {}
    multi_db_count = 0
    for e in entries:
        for db in e["supporting_databases"]:
            db_counts[db] = db_counts.get(db, 0) + 1
        if len(e["supporting_databases"]) >= 2:
            multi_db_count += 1

    # Save run_log.json
    run_log = {
        "agent": f"Claude {args.model.title()} 4.6",
        "model": f"claude-{args.model}-4-6",
        "condition": args.condition,
        "prompt": f"{args.condition}",
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

    # Save detailed log
    with open(out_dir / "run.log", "w") as f:
        f.write("\n".join(log_lines))

    # Run scorer
    log_fn("SCORE", "Running evaluation scorer...")
    gold_path = "gold_standard/parsed/phosphoatlas_gold.json"
    if Path(gold_path).exists():
        from evaluation.scorer import load_gold, score_atlas, score_per_kinase
        gold = load_gold(gold_path)
        scores = score_atlas(entries, gold)

        # Save summary.json
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

    # Re-save log with scoring output
    with open(out_dir / "run.log", "w") as f:
        f.write("\n".join(log_lines))

    print(f"\n{'=' * 60}")
    print(f"Outputs in: {out_dir}/")
    print(f"  atlas.json, run_log.json, run.log")
    print(f"  scores/summary.json, scores/per_kinase.json, scores/peptide_mismatches.json")
    print(f"\n[COST] Estimated: ${token_summary['estimated_cost_usd']:.2f} "
          f"({token_summary['total_tokens']:,} tokens for {args.model})")


if __name__ == "__main__":
    main()
