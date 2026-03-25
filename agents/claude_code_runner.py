#!/usr/bin/env python3
"""
Claude Code Agent Runner for PhosphoAtlas Benchmark.

Instead of calling the Anthropic API, this runner executes the paper_informed
strategy as Claude Code would — systematically querying all databases through
the tool interface, logging every call, and producing a deduplicated atlas.

Usage:
  python3 agents/claude_code_runner.py --condition paper_informed
"""
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from databases.tools import DatabaseTools


def run_paper_informed(tools: DatabaseTools, log_fn) -> list[dict]:
    """Execute the paper_informed curation strategy.

    Following the prompt's instructions:
    1. Discover available databases
    2. Develop systematic curation strategy
    3. Exhaustively query all kinase-substrate-site triplets
    4. Cross-reference across databases
    5. Submit deduplicated atlas
    """
    atlas = {}  # key: "KINASE|SUBSTRATE|SITE" -> entry dict

    def add_entry(kinase, substrate, site, uniprot="", peptide="", source=""):
        """Add or merge an entry into the atlas."""
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

    # === Step 1: Discover databases ===
    log_fn("DISCOVER", "Listing available databases...")
    dbs = tools.list_databases()
    db_ids = [d["id"] for d in dbs["databases"]]
    log_fn("DISCOVER", f"Found {len(db_ids)} databases: {db_ids}")

    # === Step 2: Get stats to understand scope ===
    for db_id in db_ids:
        stats = tools.get_stats(db_id)
        log_fn("STATS", f"{db_id}: {stats['total_entries']} entries, "
               f"{stats['unique_kinases']} kinases, {stats['unique_substrates']} substrates")

    # === Step 3: Systematically extract ALL entries from each database ===

    # --- PSP: Query every kinase ---
    log_fn("CURATE", "=== PhosphoSitePlus: exhaustive kinase-by-kinase extraction ===")
    psp_kinases = []
    offset = 0
    while True:
        result = tools.list_kinases("psp", offset=offset, limit=100)
        batch = result["kinases"]
        psp_kinases.extend(batch)
        log_fn("CURATE", f"  PSP kinases page offset={offset}: got {len(batch)}, "
               f"total so far={len(psp_kinases)}/{result['total_kinases']}")
        if len(batch) < 100 or len(psp_kinases) >= result["total_kinases"]:
            break
        offset += 100

    for i, kinase in enumerate(psp_kinases):
        result = tools.query_by_kinase("psp", kinase)
        for e in result["entries"]:
            add_entry(
                e.get("kinase_gene", ""),
                e.get("substrate_gene", ""),
                e.get("phospho_site", ""),
                e.get("substrate_uniprot", ""),
                e.get("heptameric_peptide", ""),
                "PhosphoSitePlus",
            )
        if (i + 1) % 50 == 0:
            log_fn("CURATE", f"  PSP progress: {i+1}/{len(psp_kinases)} kinases, "
                   f"atlas size={len(atlas)}")

    log_fn("CURATE", f"  PSP complete: {len(atlas)} unique triplets after PSP")

    # --- SIGNOR: Query every kinase ---
    log_fn("CURATE", "=== SIGNOR: exhaustive kinase-by-kinase extraction ===")
    signor_kinases = []
    offset = 0
    while True:
        result = tools.list_kinases("signor", offset=offset, limit=100)
        batch = result["kinases"]
        signor_kinases.extend(batch)
        log_fn("CURATE", f"  SIGNOR kinases page offset={offset}: got {len(batch)}, "
               f"total so far={len(signor_kinases)}/{result['total_kinases']}")
        if len(batch) < 100 or len(signor_kinases) >= result["total_kinases"]:
            break
        offset += 100

    before_signor = len(atlas)
    for i, kinase in enumerate(signor_kinases):
        result = tools.query_by_kinase("signor", kinase)
        for e in result["entries"]:
            add_entry(
                e.get("kinase_gene", ""),
                e.get("substrate_gene", ""),
                e.get("phospho_site", ""),
                "",
                e.get("heptameric_peptide", ""),
                "SIGNOR",
            )
        if (i + 1) % 50 == 0:
            log_fn("CURATE", f"  SIGNOR progress: {i+1}/{len(signor_kinases)} kinases, "
                   f"atlas size={len(atlas)}")

    new_from_signor = len(atlas) - before_signor
    log_fn("CURATE", f"  SIGNOR complete: +{new_from_signor} new, {len(atlas)} total")

    # --- UniProt: Query every kinase ---
    log_fn("CURATE", "=== UniProt: exhaustive kinase-by-kinase extraction ===")
    uniprot_kinases = []
    offset = 0
    while True:
        result = tools.list_kinases("uniprot", offset=offset, limit=100)
        batch = result["kinases"]
        uniprot_kinases.extend(batch)
        log_fn("CURATE", f"  UniProt kinases page offset={offset}: got {len(batch)}, "
               f"total so far={len(uniprot_kinases)}/{result['total_kinases']}")
        if len(batch) < 100 or len(uniprot_kinases) >= result["total_kinases"]:
            break
        offset += 100

    before_uniprot = len(atlas)
    for i, kinase in enumerate(uniprot_kinases):
        result = tools.query_by_kinase("uniprot", kinase)
        for e in result["entries"]:
            add_entry(
                e.get("kinase_gene", ""),
                e.get("substrate_gene", ""),
                e.get("phospho_site", ""),
                e.get("substrate_uniprot", ""),
                "",
                "UniProt",
            )
        if (i + 1) % 50 == 0:
            log_fn("CURATE", f"  UniProt progress: {i+1}/{len(uniprot_kinases)} kinases, "
                   f"atlas size={len(atlas)}")

    new_from_uniprot = len(atlas) - before_uniprot
    log_fn("CURATE", f"  UniProt complete: +{new_from_uniprot} new, {len(atlas)} total")

    # === Step 4: Cross-reference — also query by SUBSTRATE to catch entries ===
    # Some entries may only appear when querying by substrate (if the kinase
    # name differs across databases). Query all substrates too.
    log_fn("XREF", "=== Cross-referencing: querying by substrate across all DBs ===")

    for db_id in db_ids:
        substrates = []
        offset = 0
        while True:
            result = tools.list_substrates(db_id, offset=offset, limit=100)
            batch = result["substrates"]
            substrates.extend(batch)
            if len(batch) < 100 or len(substrates) >= result["total_substrates"]:
                break
            offset += 100

        before = len(atlas)
        for i, substrate in enumerate(substrates):
            result = tools.query_by_substrate(db_id, substrate)
            for e in result["entries"]:
                source_name = {"psp": "PhosphoSitePlus", "signor": "SIGNOR", "uniprot": "UniProt"}[db_id]
                add_entry(
                    e.get("kinase_gene", ""),
                    e.get("substrate_gene", ""),
                    e.get("phospho_site", ""),
                    e.get("substrate_uniprot", ""),
                    e.get("heptameric_peptide", ""),
                    source_name,
                )
        gained = len(atlas) - before
        log_fn("XREF", f"  {db_id} substrate sweep: +{gained} new entries")

    log_fn("RESULT", f"=== Final atlas: {len(atlas)} unique kinase-substrate-site triplets ===")

    # Sort entries for reproducibility
    entries = sorted(atlas.values(), key=lambda e: (e["kinase_gene"], e["substrate_gene"], e["phospho_site"]))

    # Summary stats
    multi_db = sum(1 for e in entries if len(e["supporting_databases"]) >= 2)
    kinases = set(e["kinase_gene"] for e in entries)
    substrates = set(e["substrate_gene"] for e in entries)
    with_uniprot = sum(1 for e in entries if e["substrate_uniprot"])
    with_peptide = sum(1 for e in entries if e["heptameric_peptide"])

    log_fn("RESULT", f"  Unique kinases:    {len(kinases)}")
    log_fn("RESULT", f"  Unique substrates: {len(substrates)}")
    log_fn("RESULT", f"  Multi-DB support:  {multi_db} ({multi_db/max(len(entries),1)*100:.1f}%)")
    log_fn("RESULT", f"  With UniProt ID:   {with_uniprot}")
    log_fn("RESULT", f"  With peptide:      {with_peptide}")

    return entries


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Claude Code Agent Runner")
    parser.add_argument("--condition", default="paper_informed", choices=["paper_informed"])
    parser.add_argument("--databases-dir", default="databases")
    parser.add_argument("--output-dir", default="results/raw")
    parser.add_argument("--run-id", type=int, default=0)
    args = parser.parse_args()

    # Setup
    tools = DatabaseTools(args.databases_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log_lines = []

    def log_fn(phase, msg):
        line = f"[{time.strftime('%H:%M:%S')}][{phase}] {msg}"
        log_lines.append(line)
        print(line, flush=True)

    # Load prompt
    prompt_path = Path(f"agents/prompts/{args.condition}.txt")
    prompt = prompt_path.read_text()
    log_fn("SETUP", f"Loaded prompt: {prompt_path} ({len(prompt)} chars)")
    log_fn("SETUP", f"Condition: {args.condition}, Run ID: {args.run_id}")

    # Run
    t0 = time.time()
    entries = run_paper_informed(tools, log_fn)
    elapsed = time.time() - t0

    log_fn("DONE", f"Elapsed: {elapsed:.1f}s ({elapsed/60:.1f}m)")
    log_fn("DONE", f"Tool calls: {tools.call_count}")

    # Save atlas
    atlas_path = out_dir / f"claude-code_{args.condition}_run{args.run_id}.json"
    with open(atlas_path, "w") as f:
        json.dump(entries, f, indent=2)
    log_fn("SAVE", f"Atlas saved: {atlas_path} ({len(entries)} entries)")

    # Save run metadata
    meta = {
        "model": "claude-code",
        "condition": args.condition,
        "run_id": args.run_id,
        "prompt_file": str(prompt_path),
        "atlas_size": len(entries),
        "tool_calls": tools.call_count,
        "elapsed_seconds": round(elapsed, 1),
        "tool_log": tools.call_log,
    }
    meta_path = out_dir / f"claude-code_{args.condition}_run{args.run_id}_meta.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    # Save run log
    log_path = out_dir / f"claude-code_{args.condition}_run{args.run_id}.log"
    with open(log_path, "w") as f:
        f.write("\n".join(log_lines))

    print(f"\nOutputs:")
    print(f"  Atlas:    {atlas_path}")
    print(f"  Metadata: {meta_path}")
    print(f"  Log:      {log_path}")


if __name__ == "__main__":
    main()
