#!/usr/bin/env python3
"""
Claude Code Agent Runner for PhosphoAtlas Benchmark.

Executes curation strategies by systematically querying databases through
the tool interface, logging every call, and producing a deduplicated atlas.

Each condition maps to a distinct strategy function that follows a different
approach to atlas construction.

Usage:
  python3 agents/claude_code_runner.py --condition paper_informed
  python3 agents/claude_code_runner.py --condition pipeline_guided
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from databases.tools import DatabaseTools


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


def _paginate_list(tools, method, db_id, field, log_fn, phase=""):
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


# ── Strategy: paper_informed ────────────────────────────────────────────────

def run_paper_informed(tools: DatabaseTools, log_fn) -> list[dict]:
    """Paper-informed: knows PA paper background, systematic but no pipeline steps."""
    atlas, add_entry = _make_atlas_dict()

    log_fn("DISCOVER", "Listing available databases...")
    dbs = tools.list_databases()
    db_ids = [d["id"] for d in dbs["databases"]]
    log_fn("DISCOVER", f"Found {len(db_ids)} databases: {db_ids}")

    for db_id in db_ids:
        stats = tools.get_stats(db_id)
        log_fn("STATS", f"{db_id}: {stats['total_entries']} entries, "
               f"{stats['unique_kinases']} kinases, {stats['unique_substrates']} substrates")

    # Extract by kinase from each DB
    for db_id in db_ids:
        source = DB_SOURCE_NAMES[db_id]
        log_fn("CURATE", f"=== {source}: exhaustive kinase extraction ===")
        kinases = _paginate_list(tools, "list_kinases", db_id, "kinases", log_fn, "CURATE")
        before = len(atlas)
        for i, kinase in enumerate(kinases):
            result = tools.query_by_kinase(db_id, kinase)
            _extract_from_entries(result["entries"], source, add_entry)
            if (i + 1) % 50 == 0:
                log_fn("CURATE", f"  {db_id} kinase progress: {i + 1}/{len(kinases)}, atlas={len(atlas)}")
        log_fn("CURATE", f"  {db_id} kinase done: +{len(atlas) - before} new, {len(atlas)} total")

    # Cross-reference by substrate
    log_fn("XREF", "=== Cross-referencing: substrate sweep ===")
    for db_id in db_ids:
        source = DB_SOURCE_NAMES[db_id]
        substrates = _paginate_list(tools, "list_substrates", db_id, "substrates", log_fn, "XREF")
        before = len(atlas)
        for substrate in substrates:
            result = tools.query_by_substrate(db_id, substrate)
            _extract_from_entries(result["entries"], source, add_entry)
        log_fn("XREF", f"  {db_id} substrate sweep: +{len(atlas) - before} new")

    log_fn("RESULT", "=== Final atlas ===")
    return _finalize(atlas, log_fn)


# ── Strategy: pipeline_guided (Olow et al. pipeline) ───────────────────────

def run_pipeline_guided(tools: DatabaseTools, log_fn) -> list[dict]:
    """Pipeline-guided: follows the exact Olow et al. (2016) multi-phase pipeline.

    Phase 1: Harmonize — discover databases, build protein reference index
    Phase 2: Build phosphorylation relational DB — systematic extraction
    Phase 3: Cross-reference and quality control
    """
    atlas, add_entry = _make_atlas_dict()

    # ── PHASE 1: Harmonize and Centralize ──────────────────────────────────

    log_fn("PHASE1", "=" * 60)
    log_fn("PHASE1", "PHASE 1: HARMONIZE AND CENTRALIZE PROTEIN DATA")
    log_fn("PHASE1", "=" * 60)

    # Step 1: Build Protein Reference Index — discover all databases
    log_fn("STEP1", "Step 1: Build Protein Reference Index (discover databases)")
    dbs = tools.list_databases()
    db_ids = [d["id"] for d in dbs["databases"]]
    for d in dbs["databases"]:
        log_fn("STEP1", f"  Database: {d['name']} ({d['id']}) — {d['description']}")

    for db_id in db_ids:
        stats = tools.get_stats(db_id)
        log_fn("STEP1", f"  {db_id} scope: {stats['total_entries']} entries, "
               f"{stats['unique_kinases']} kinases, {stats['unique_substrates']} substrates")

    # Step 2: Curation 1 — Cross-reference all databases
    # Build complete kinase and substrate lists from ALL databases
    log_fn("STEP2", "Step 2: Curation 1 — Cross-reference external databases")
    all_kinases_by_db = {}
    all_substrates_by_db = {}
    global_kinases = set()
    global_substrates = set()

    for db_id in db_ids:
        kinases = _paginate_list(tools, "list_kinases", db_id, "kinases", log_fn, "STEP2")
        substrates = _paginate_list(tools, "list_substrates", db_id, "substrates", log_fn, "STEP2")
        all_kinases_by_db[db_id] = kinases
        all_substrates_by_db[db_id] = substrates
        global_kinases.update(kinases)
        global_substrates.update(substrates)

    log_fn("STEP2", f"  Global kinase inventory: {len(global_kinases)} unique across all DBs")
    log_fn("STEP2", f"  Global substrate inventory: {len(global_substrates)} unique across all DBs")

    # Identify cross-database kinases (appear in 2+ DBs)
    kinase_db_count = {}
    for db_id, kinases in all_kinases_by_db.items():
        for k in kinases:
            kinase_db_count[k] = kinase_db_count.get(k, 0) + 1
    multi_db_kinases = sum(1 for v in kinase_db_count.values() if v >= 2)
    log_fn("STEP2", f"  Kinases in 2+ databases: {multi_db_kinases} (cross-validated)")

    # Step 3: Curation 2 — validation will happen during assembly
    log_fn("STEP3", "Step 3: Curation 2 — Consolidation deferred to assembly phase")

    # ── PHASE 2: Build Relational Database of Phosphorylation Events ───────

    log_fn("PHASE2", "=" * 60)
    log_fn("PHASE2", "PHASE 2: BUILD RELATIONAL DATABASE OF PHOSPHORYLATION EVENTS")
    log_fn("PHASE2", "=" * 60)

    # Step 4: Functional Triage — systematic extraction by kinase role
    log_fn("STEP4", "Step 4: Functional Triage — extract kinase→substrate relationships")

    for db_id in db_ids:
        source = DB_SOURCE_NAMES[db_id]
        kinases = all_kinases_by_db[db_id]
        log_fn("STEP4", f"  === {source}: querying {len(kinases)} kinases ===")
        before = len(atlas)
        for i, kinase in enumerate(kinases):
            result = tools.query_by_kinase(db_id, kinase)
            _extract_from_entries(result["entries"], source, add_entry)
            if (i + 1) % 100 == 0:
                log_fn("STEP4", f"    {source} kinase progress: {i + 1}/{len(kinases)}, atlas={len(atlas)}")
        gained = len(atlas) - before
        log_fn("STEP4", f"  {source} kinase extraction: +{gained} entries, atlas={len(atlas)}")

    # Also query by substrate role — catches entries where kinase name varies
    log_fn("STEP4", "  --- Substrate-side extraction (catch asymmetric entries) ---")
    for db_id in db_ids:
        source = DB_SOURCE_NAMES[db_id]
        substrates = all_substrates_by_db[db_id]
        before = len(atlas)
        for i, substrate in enumerate(substrates):
            result = tools.query_by_substrate(db_id, substrate)
            _extract_from_entries(result["entries"], source, add_entry)
            if (i + 1) % 100 == 0:
                log_fn("STEP4", f"    {source} substrate progress: {i + 1}/{len(substrates)}, atlas={len(atlas)}")
        gained = len(atlas) - before
        log_fn("STEP4", f"  {source} substrate extraction: +{gained} new, atlas={len(atlas)}")

    # Step 5: Curation 3 — validate and filter
    log_fn("STEP5", "Step 5: Curation 3 — Validate phosphorylation sites")

    # QC: remove entries with empty fields (exclusion criteria)
    pre_qc = len(atlas)
    to_remove = []
    for key, entry in atlas.items():
        if not entry["kinase_gene"] or not entry["substrate_gene"] or not entry["phospho_site"]:
            to_remove.append(key)
    for key in to_remove:
        del atlas[key]
    log_fn("STEP5", f"  QC: removed {len(to_remove)} entries with missing fields "
           f"({pre_qc} → {len(atlas)})")

    # Log HPS coverage
    with_hps = sum(1 for e in atlas.values() if e["heptameric_peptide"])
    log_fn("STEP5", f"  Heptameric peptide coverage: {with_hps}/{len(atlas)} "
           f"({with_hps / max(len(atlas), 1) * 100:.1f}%)")

    # ── PHASE 3: Cross-Reference and Quality Control ───────────────────────

    log_fn("PHASE3", "=" * 60)
    log_fn("PHASE3", "PHASE 3: CROSS-REFERENCING AND QUALITY CONTROL")
    log_fn("PHASE3", "=" * 60)

    # Step 7: Multi-database cross-reference stats
    log_fn("STEP7", "Step 7: Multi-database cross-reference analysis")
    multi_db = sum(1 for e in atlas.values() if len(e["supporting_databases"]) >= 2)
    triple_db = sum(1 for e in atlas.values() if len(e["supporting_databases"]) >= 3)
    log_fn("STEP7", f"  Entries in 2+ databases: {multi_db}")
    log_fn("STEP7", f"  Entries in 3  databases: {triple_db}")

    # Per-database contribution
    db_counts = {}
    for entry in atlas.values():
        for db in entry["supporting_databases"]:
            db_counts[db] = db_counts.get(db, 0) + 1
    for db_name, count in sorted(db_counts.items()):
        log_fn("STEP7", f"  {db_name}: {count} entries")

    # Step 8: Final QC and assembly
    log_fn("STEP8", "Step 8: Final Quality Control and Assembly")
    log_fn("STEP8", "  Deduplication: atlas keyed by (kinase|substrate|site) — inherently deduplicated")

    # Step 6: Assemble final PhosphoAtlas
    log_fn("STEP6", "Step 6: Assemble PhosphoAtlas Relational Database")
    log_fn("RESULT", "=== FINAL ATLAS ===")
    return _finalize(atlas, log_fn)


# ── Main entry point ───────────────────────────────────────────────────────

STRATEGIES = {
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
    parser.add_argument("--run-id", type=int, default=0)
    args = parser.parse_args()

    # Fresh tools instance — no reuse from previous runs
    tools = DatabaseTools(args.databases_dir)

    out_dir = Path(args.output_dir) if args.output_dir else Path(f"contributions/claude_opus_{args.condition}")
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
    log_fn("SETUP", f"Condition: {args.condition}, Run ID: {args.run_id}")
    log_fn("SETUP", f"Output: {out_dir}")

    # Run strategy
    t0 = time.time()
    strategy_fn = STRATEGIES[args.condition]
    entries = strategy_fn(tools, log_fn)
    elapsed = time.time() - t0

    log_fn("DONE", f"Elapsed: {elapsed:.1f}s ({elapsed / 60:.1f}m)")
    log_fn("DONE", f"Tool calls: {tools.call_count}")

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
        "agent": "Claude Opus 4.6",
        "prompt": f"{args.condition}",
        "strategy": prompt[:300].replace("\n", " ").strip() + "...",
        "databases_accessed": sorted(db_counts.keys()),
        "tool_calls": {
            "total": tools.call_count,
        },
        "raw_counts": {
            "PSP": db_counts.get("PhosphoSitePlus", 0),
            "SIGNOR": db_counts.get("SIGNOR", 0),
            "UniProt": db_counts.get("UniProt", 0),
        },
        "merged_atlas": len(entries),
        "unique_kinases": len(set(e["kinase_gene"] for e in entries)),
        "unique_substrates": len(set(e["substrate_gene"] for e in entries)),
        "multi_db_entries": multi_db_count,
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

        # Save per_kinase.json
        per_kinase = score_per_kinase(entries, gold)
        with open(scores_dir / "per_kinase.json", "w") as f:
            json.dump(per_kinase, f, indent=2)

        # Save peptide_mismatches.json
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


if __name__ == "__main__":
    main()
