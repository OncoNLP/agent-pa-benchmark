#!/usr/bin/env python3
"""
Gold Standard Mismatch Analyzer.

Identifies and categorizes false negatives into:
  A) Gene alias mismatches — kinase exists in DBs under a different symbol
  B) Attribution mismatches — substrate+site exists but attributed to a different kinase
  C) Truly absent — neither kinase nor substrate+site found in any database

For each mismatch, provides database evidence (exact entries, tool queries) so
findings can be independently verified.

Usage:
  python3 -m evaluation.gold_mismatch_analyzer \
      --atlas contributions/claude_opus_paper_informed/atlas.json \
      --gold gold_standard/parsed/phosphoatlas_gold.json \
      --databases-dir databases \
      --output contributions/claude_opus_paper_informed/mismatch_analysis/
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from databases.tools import DatabaseTools
from evaluation.normalizer import (
    GENE_ALIASES,
    make_triplet_key,
    normalize_gene_symbol,
    normalize_phospho_site,
)
from evaluation.scorer import load_atlas, load_gold


def _query_substrate_site_evidence(tools: DatabaseTools, substrate: str, site: str) -> list[dict]:
    """Query all DBs for a substrate+site, return evidence records."""
    evidence = []
    for db_id in ("psp", "signor", "uniprot"):
        result = tools.query_by_site(db_id, substrate, site)
        for entry in result.get("entries", []):
            evidence.append({
                "database": db_id,
                "kinase_gene": entry.get("kinase_gene", ""),
                "substrate_gene": entry.get("substrate_gene", ""),
                "phospho_site": entry.get("phospho_site", ""),
                "substrate_uniprot": entry.get("substrate_uniprot", ""),
                "heptameric_peptide": entry.get("heptameric_peptide", ""),
                "in_vivo": entry.get("in_vivo", ""),
                "in_vitro": entry.get("in_vitro", ""),
                "pubmed_id": entry.get("pubmed_id", ""),
                "mechanism": entry.get("mechanism", ""),
            })
    return evidence


def _query_kinase_aliases(tools: DatabaseTools, kinase: str, aliases: list[str]) -> dict:
    """Check if a kinase or its aliases exist in any database."""
    results = {}
    for name in [kinase] + aliases:
        for db_id in ("psp", "signor", "uniprot"):
            as_kinase = tools.query_by_kinase(db_id, name)
            if as_kinase["total_entries"] > 0:
                results.setdefault(name, []).append({
                    "database": db_id,
                    "role": "kinase",
                    "entry_count": as_kinase["total_entries"],
                    "sample_substrates": [
                        f"{e['substrate_gene']} {e['phospho_site']}"
                        for e in as_kinase["entries"][:5]
                    ],
                })
            search_result = tools.search(db_id, name, limit=3)
            if search_result["total_matches"] > 0:
                results.setdefault(name, []).append({
                    "database": db_id,
                    "role": "search_hit",
                    "match_count": search_result["total_matches"],
                    "sample": [
                        f"{e.get('kinase_gene','?')}→{e.get('substrate_gene','?')} {e.get('phospho_site','?')}"
                        for e in search_result["results"][:3]
                    ],
                })
    return results


# Known aliases for kinases commonly found under different symbols
KNOWN_ALIASES = {
    "ADRBK2": ["GRK3", "BARK2"],
    "MYT1": ["PKMYT1"],
    "PAK7": ["PAK5"],
    "PKD1": ["PRKD1"],
    "ERBB3": ["HER3"],
    "FCGR3A": ["CD16A", "CD16"],
    "PDIK1L": [],
    "PEG3": ["PW1"],
    "PRKAR1A": [],
    "PRKRIR": ["EIF2AK1P1"],
    "PRKY": [],
    "RAD17": [],
    "RPS6KC1": [],
    "SHB": [],
    "BCAT2": [],
    "EPHB4": [],
    "TTN": [],
    "CAD": [],
}


def analyze_mismatches(
    atlas_path: str,
    gold_path: str,
    databases_dir: str,
    output_dir: str,
):
    """Run full mismatch analysis with database evidence."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load data
    print("Loading gold standard...")
    gold = load_gold(gold_path)
    gold_raw = json.load(open(gold_path))

    print("Loading agent atlas...")
    atlas = load_atlas(atlas_path)

    print("Initializing database tools...")
    tools = DatabaseTools(databases_dir)

    # Build agent lookup by normalized substrate+site
    agent_by_sub_site = defaultdict(list)
    for e in atlas:
        sub = normalize_gene_symbol(e.get("substrate_gene", ""))
        site = normalize_phospho_site(e.get("phospho_site", ""))
        agent_by_sub_site[f"{sub}|{site}"].append(e)

    # Build agent lookup by normalized triplet
    agent_triplets = set()
    for e in atlas:
        key = make_triplet_key(
            e.get("kinase_gene", ""),
            e.get("substrate_gene", ""),
            e.get("phospho_site", ""),
        )
        agent_triplets.add(key)

    # Identify missed kinases: gold kinases with ZERO matched triplets
    # (not just kinase name absence — a kinase name can appear in the atlas
    # without matching any gold triplet, e.g. EPHB4→NOS3 with empty site)
    gold_triplets = gold["triplet_keys"]
    matched_kinases = set()
    for e in atlas:
        key = make_triplet_key(
            e.get("kinase_gene", ""),
            e.get("substrate_gene", ""),
            e.get("phospho_site", ""),
        )
        if key in gold_triplets:
            matched_kinases.add(normalize_gene_symbol(e.get("kinase_gene", "")))
    missed_kinases = sorted(gold["kinase_names"] - matched_kinases)
    print(f"Missed kinases (zero matched triplets): {len(missed_kinases)}")

    # Categorize each missed kinase
    alias_mismatches = []
    attribution_mismatches = []
    truly_absent = []

    for kinase in missed_kinases:
        gold_entries = gold_raw["kinases"].get(kinase, {}).get("entries", [])
        if not gold_entries:
            continue

        norm_kinase = normalize_gene_symbol(kinase)
        aliases = KNOWN_ALIASES.get(kinase, [])

        # Check if kinase exists under an alias in any DB
        alias_evidence = _query_kinase_aliases(tools, kinase, aliases)

        # For each gold entry, check if the substrate+site exists in agent atlas
        entry_analysis = []
        for ge in gold_entries:
            sub = normalize_gene_symbol(ge["substrate_gene"])
            site = normalize_phospho_site(ge["phospho_site"])
            sub_site_key = f"{sub}|{site}"

            # Query databases directly for this substrate+site
            db_evidence = _query_substrate_site_evidence(
                tools, ge["substrate_gene"], ge["phospho_site"]
            )

            # Check if agent has this substrate+site under any kinase
            agent_matches = agent_by_sub_site.get(sub_site_key, [])
            agent_kinases_for_entry = sorted(set(
                normalize_gene_symbol(ae["kinase_gene"]) for ae in agent_matches
            ))

            entry_analysis.append({
                "gold_kinase": kinase,
                "gold_kinase_normalized": norm_kinase,
                "substrate_gene": ge["substrate_gene"],
                "substrate_uniprot": ge.get("substrate_uniprot", ""),
                "phospho_site": ge["phospho_site"],
                "heptameric_peptide": ge.get("heptameric_peptide", ""),
                "pa_version": ge.get("pa_version", ""),
                "in_agent_atlas": len(agent_matches) > 0,
                "agent_kinases": agent_kinases_for_entry,
                "database_evidence": db_evidence,
                "database_kinases": sorted(set(
                    e["kinase_gene"] for e in db_evidence
                )),
            })

        found_in_dbs = any(ea["database_evidence"] for ea in entry_analysis)
        found_in_agent = any(ea["in_agent_atlas"] for ea in entry_analysis)
        all_in_agent = all(ea["in_agent_atlas"] for ea in entry_analysis)

        record = {
            "kinase": kinase,
            "kinase_normalized": norm_kinase,
            "gold_entry_count": len(gold_entries),
            "kinase_uniprot": gold_entries[0].get("kinase_uniprot", ""),
            "alias_evidence": alias_evidence,
            "entries": entry_analysis,
        }

        if found_in_agent or found_in_dbs:
            # Substrate+site exists — it's an attribution mismatch
            entries_in_agent = sum(1 for ea in entry_analysis if ea["in_agent_atlas"])
            entries_in_dbs = sum(1 for ea in entry_analysis if ea["database_evidence"])
            record["entries_found_in_agent"] = entries_in_agent
            record["entries_found_in_dbs"] = entries_in_dbs
            record["entries_truly_absent"] = len(entry_analysis) - max(entries_in_agent, entries_in_dbs)
            record["category"] = "attribution_mismatch"
            attribution_mismatches.append(record)
        else:
            record["category"] = "truly_absent"
            truly_absent.append(record)

    # ── Save outputs ────────────────────────────────────────────────────────

    # 1. Attribution mismatches — full evidence
    attrib_path = output_dir / "attribution_mismatches.json"
    with open(attrib_path, "w") as f:
        json.dump(attribution_mismatches, f, indent=2)
    print(f"Saved: {attrib_path} ({len(attribution_mismatches)} kinases)")

    # 2. Truly absent
    absent_path = output_dir / "truly_absent.json"
    with open(absent_path, "w") as f:
        json.dump(truly_absent, f, indent=2)
    print(f"Saved: {absent_path} ({len(truly_absent)} kinases)")

    # 3. Human-readable summary
    summary_lines = []
    summary_lines.append("=" * 90)
    summary_lines.append("GOLD STANDARD MISMATCH ANALYSIS")
    summary_lines.append("=" * 90)
    summary_lines.append("")
    summary_lines.append(f"Total missed kinases:      {len(missed_kinases)}")
    summary_lines.append(f"Attribution mismatches:    {len(attribution_mismatches)}")
    summary_lines.append(f"Truly absent:              {len(truly_absent)}")
    summary_lines.append("")

    summary_lines.append("-" * 90)
    summary_lines.append("ATTRIBUTION MISMATCHES")
    summary_lines.append("Gold standard says kinase=X, databases say kinase=Y for same substrate+site")
    summary_lines.append("-" * 90)

    total_attrib_entries = 0
    for rec in attribution_mismatches:
        k = rec["kinase"]
        n = rec["gold_entry_count"]
        total_attrib_entries += n
        summary_lines.append("")
        summary_lines.append(f"  {k} (UniProt: {rec.get('kinase_uniprot','?')}, {n} gold entries)")
        if rec["alias_evidence"]:
            summary_lines.append(f"    Alias evidence:")
            for alias_name, hits in rec["alias_evidence"].items():
                for h in hits:
                    if h["role"] == "kinase":
                        summary_lines.append(
                            f"      {alias_name} in {h['database']}: "
                            f"{h['entry_count']} entries as kinase "
                            f"(e.g. {', '.join(h['sample_substrates'][:3])})"
                        )
        for ea in rec["entries"]:
            sub = ea["substrate_gene"]
            site = ea["phospho_site"]
            if ea["in_agent_atlas"]:
                summary_lines.append(
                    f"    {sub:>12} {site:<8} "
                    f"AGENT HAS under: {', '.join(ea['agent_kinases'])}"
                )
                # Show DB evidence
                db_kinases_by_db = defaultdict(list)
                for ev in ea["database_evidence"]:
                    db_kinases_by_db[ev["database"]].append(ev["kinase_gene"])
                for db, kins in db_kinases_by_db.items():
                    summary_lines.append(
                        f"                          "
                        f"  {db}: {', '.join(sorted(set(kins)))}"
                    )
            else:
                summary_lines.append(
                    f"    {sub:>12} {site:<8} "
                    f"NOT IN AGENT ATLAS OR DBS"
                )

    summary_lines.append("")
    summary_lines.append(f"  Total attribution-mismatch entries: {total_attrib_entries}")

    summary_lines.append("")
    summary_lines.append("-" * 90)
    summary_lines.append("TRULY ABSENT")
    summary_lines.append("Neither kinase nor substrate+site found in any database")
    summary_lines.append("-" * 90)

    total_absent_entries = 0
    for rec in truly_absent:
        k = rec["kinase"]
        n = rec["gold_entry_count"]
        total_absent_entries += n
        summary_lines.append(f"  {k} (UniProt: {rec.get('kinase_uniprot','?')}, {n} gold entries)")
        for ea in rec["entries"]:
            summary_lines.append(f"    {ea['substrate_gene']:>12} {ea['phospho_site']:<8}")

    summary_lines.append("")
    summary_lines.append(f"  Total truly absent entries: {total_absent_entries}")

    summary_lines.append("")
    summary_lines.append("=" * 90)
    summary_lines.append("RECALL CEILING")
    summary_lines.append("=" * 90)

    total_gold = gold_raw["metadata"]["total_entries"]
    summary_lines.append(f"  Gold standard entries:          {total_gold}")
    summary_lines.append(f"  Attribution mismatch entries:   {total_attrib_entries}")
    summary_lines.append(f"  Truly absent entries:           {total_absent_entries}")
    summary_lines.append(f"  Max possible recall:            {round((total_gold - total_absent_entries) / total_gold, 4)}")
    summary_lines.append(f"  Unreachable:                    {total_absent_entries} ({round(total_absent_entries / total_gold * 100, 3)}%)")

    summary_text = "\n".join(summary_lines)
    summary_path = output_dir / "summary.txt"
    with open(summary_path, "w") as f:
        f.write(summary_text)
    print(f"Saved: {summary_path}")
    print(summary_text)


def main():
    parser = argparse.ArgumentParser(
        description="Analyze gold standard mismatches with database evidence",
    )
    parser.add_argument("--atlas", required=True, help="Path to agent atlas JSON")
    parser.add_argument("--gold", required=True, help="Path to gold standard JSON")
    parser.add_argument("--databases-dir", default="databases", help="Path to database files")
    parser.add_argument("--output", required=True, help="Output directory for analysis")
    args = parser.parse_args()

    analyze_mismatches(args.atlas, args.gold, args.databases_dir, args.output)


if __name__ == "__main__":
    main()
