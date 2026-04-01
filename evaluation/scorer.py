#!/usr/bin/env python3
"""
Score an agent-produced atlas against the PhosphoAtlas 2.0 gold standard.

Metrics:
  - Triplet-level: precision, recall, F1 on (kinase, substrate, site) matching
  - Column-level: accuracy of SUB_MOD_RSD, SITE_+/-7_AA, SUB_ACC_ID for matched entries
  - Kinase discovery: how many gold-standard kinases the agent found
  - Cross-referencing: fraction of entries supported by multiple databases
  - Per-kinase breakdown: precision/recall per kinase (for tier analysis)

Usage:
  python -m evaluation.scorer \
      --atlas results/raw/atlas_claude_opus_naive_run0.json \
      --gold gold_standard/parsed/phosphoatlas_gold.json \
      --output results/scores/claude_opus_naive_run0
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evaluation.normalizer import (
    make_triplet_key,
    normalize_gene_symbol,
    normalize_phospho_site,
)


def load_gold(gold_path: str) -> dict:
    """Load gold standard and build lookup structures."""
    with open(gold_path) as f:
        gold = json.load(f)

    entries = []
    triplet_keys = set()
    by_kinase = defaultdict(list)

    for kinase_gene, kinase_data in gold["kinases"].items():
        for e in kinase_data["entries"]:
            key = make_triplet_key(e["kinase_gene"], e["substrate_gene"], e["phospho_site"])
            entries.append(e)
            triplet_keys.add(key)
            by_kinase[normalize_gene_symbol(e["kinase_gene"])].append(e)

    return {
        "metadata": gold["metadata"],
        "entries": entries,
        "triplet_keys": triplet_keys,
        "by_kinase": dict(by_kinase),
        "kinase_names": set(by_kinase.keys()),
    }


def load_atlas(atlas_path: str) -> list[dict]:
    """Load agent atlas (JSON array of entries)."""
    with open(atlas_path) as f:
        data = json.load(f)
    # Handle both direct array and wrapped format
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "atlas" in data:
        return data["atlas"]
    if isinstance(data, dict) and "entries" in data:
        return data["entries"]
    return data


def score_triplets(agent_entries: list, gold: dict) -> dict:
    """Score at the triplet level: precision, recall, F1."""
    agent_keys = set()
    for e in agent_entries:
        key = make_triplet_key(
            e.get("kinase_gene", ""),
            e.get("substrate_gene", ""),
            e.get("phospho_site", ""),
        )
        if key and "|" in key:
            agent_keys.add(key)

    gold_keys = gold["triplet_keys"]

    tp = len(agent_keys & gold_keys)
    fp = len(agent_keys - gold_keys)
    fn = len(gold_keys - agent_keys)

    precision = round(tp / (tp + fp), 4) if (tp + fp) > 0 else 0
    recall = round(tp / (tp + fn), 4) if (tp + fn) > 0 else 0
    f1 = round(2 * precision * recall / (precision + recall), 4) if (precision + recall) > 0 else 0

    return {
        "total_agent_entries": len(agent_entries),
        "total_gold_entries": len(gold_keys),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def score_columns(agent_entries: list, gold: dict) -> dict:
    """Score column-level accuracy for matched triplets."""
    # Build gold lookup by triplet key
    gold_lookup = {}
    for e in gold["entries"]:
        key = make_triplet_key(e["kinase_gene"], e["substrate_gene"], e["phospho_site"])
        gold_lookup[key] = e

    # Build agent lookup
    agent_lookup = {}
    for e in agent_entries:
        key = make_triplet_key(
            e.get("kinase_gene", ""),
            e.get("substrate_gene", ""),
            e.get("phospho_site", ""),
        )
        if key:
            agent_lookup[key] = e

    matched_keys = set(agent_lookup.keys()) & set(gold_lookup.keys())

    # SUB_MOD_RSD (phospho-site exact match — should be 100% since it's in the key)
    site_exact = 0
    site_total = len(matched_keys)

    # SITE_+/-7_AA (heptameric peptide)
    # NOTE: Case-insensitive comparison is the primary metric because
    # lowercase letters in heptameric peptides (s, t, y) merely indicate
    # "phospho-capable residue" — a display convention that varies by
    # database (PSP vs SIGNOR vs UniProt). The amino acid identity and
    # phosphorylation site are the same regardless of case.
    peptide_exact = 0
    peptide_case_diff = 0
    peptide_missing = 0
    peptide_mismatch = 0
    peptide_total = 0
    peptide_mismatches = []

    # SUB_ACC_ID (UniProt)
    uniprot_exact = 0
    uniprot_total = 0

    for key in matched_keys:
        ge = gold_lookup[key]
        ae = agent_lookup[key]

        # Site
        if normalize_phospho_site(ae.get("phospho_site", "")) == normalize_phospho_site(ge["phospho_site"]):
            site_exact += 1

        # Peptide
        gold_pep = ge.get("heptameric_peptide", "") or ""
        agent_pep = ae.get("heptameric_peptide", "") or ""
        if gold_pep:
            peptide_total += 1
            if not agent_pep:
                peptide_missing += 1
            elif gold_pep == agent_pep:
                peptide_exact += 1
            elif gold_pep.upper() == agent_pep.upper():
                peptide_case_diff += 1
            else:
                peptide_mismatch += 1
                if len(peptide_mismatches) < 50:
                    peptide_mismatches.append({
                        "kinase": ge["kinase_gene"],
                        "substrate": ge["substrate_gene"],
                        "site": ge["phospho_site"],
                        "gold_peptide": gold_pep,
                        "agent_peptide": agent_pep,
                    })

        # UniProt
        gold_up = ge.get("substrate_uniprot", "") or ""
        agent_up = ae.get("substrate_uniprot", "") or ""
        if gold_up and agent_up:
            uniprot_total += 1
            if gold_up == agent_up:
                uniprot_exact += 1

    peptide_matched = peptide_exact + peptide_case_diff  # case-insensitive matches

    return {
        "matched_triplets": len(matched_keys),
        "site_exact": site_exact,
        "site_total": site_total,
        "site_accuracy": round(site_exact / site_total, 4) if site_total else 0,
        "peptide_exact": peptide_exact,
        "peptide_case_diff": peptide_case_diff,
        "peptide_missing": peptide_missing,
        "peptide_mismatch": peptide_mismatch,
        "peptide_total": peptide_total,
        # Primary metric: case-insensitive (biological identity match)
        "peptide_accuracy": round(peptide_matched / peptide_total, 4) if peptide_total else 0,
        # Secondary metrics for reporting
        "peptide_exact_accuracy": round(peptide_exact / peptide_total, 4) if peptide_total else 0,
        "peptide_mismatch_rate": round(peptide_mismatch / peptide_total, 4) if peptide_total else 0,
        "peptide_missing_count": peptide_missing,
        "uniprot_exact": uniprot_exact,
        "uniprot_total": uniprot_total,
        "uniprot_accuracy": round(uniprot_exact / uniprot_total, 4) if uniprot_total else 0,
        "peptide_mismatches": peptide_mismatches,
    }


def score_kinase_discovery(agent_entries: list, gold: dict) -> dict:
    """Score how many gold-standard kinases the agent discovered.

    A kinase is 'discovered' only if at least one of its gold-standard
    triplets is matched — not merely if the kinase name appears in the atlas.
    """
    gold_triplets = gold["triplet_keys"]

    # Kinases with at least one matched gold triplet
    matched_kinases = set()
    all_agent_kinases = set()
    for e in agent_entries:
        k = normalize_gene_symbol(e.get("kinase_gene", ""))
        if not k:
            continue
        all_agent_kinases.add(k)
        key = make_triplet_key(
            e.get("kinase_gene", ""),
            e.get("substrate_gene", ""),
            e.get("phospho_site", ""),
        )
        if key in gold_triplets:
            matched_kinases.add(k)

    gold_kinases = gold["kinase_names"]
    discovered = matched_kinases & gold_kinases
    missed = gold_kinases - matched_kinases
    novel = all_agent_kinases - gold_kinases

    return {
        "kinases_in_gold": len(gold_kinases),
        "kinases_discovered": len(discovered),
        "kinases_missed": len(missed),
        "kinases_novel": len(novel),
        "discovery_rate": round(len(discovered) / len(gold_kinases), 4) if gold_kinases else 0,
        "missed_kinases": sorted(missed)[:50],
        "novel_kinases": sorted(novel)[:50],
    }


def score_cross_referencing(agent_entries: list) -> dict:
    """Score how well the agent cross-referenced across databases."""
    multi_db = 0
    db_counts = defaultdict(int)
    for e in agent_entries:
        dbs = e.get("supporting_databases", [])
        if len(dbs) > 1:
            multi_db += 1
        for db in dbs:
            db_counts[db] += 1

    total = len(agent_entries) or 1
    return {
        "multi_db_count": multi_db,
        "multi_db_pct": round(multi_db / total * 100, 1),
        "db_coverage": dict(db_counts),
    }


def score_per_kinase(agent_entries: list, gold: dict) -> dict:
    """Per-kinase precision/recall breakdown."""
    agent_by_kinase = defaultdict(set)
    for e in agent_entries:
        key = make_triplet_key(
            e.get("kinase_gene", ""),
            e.get("substrate_gene", ""),
            e.get("phospho_site", ""),
        )
        k = normalize_gene_symbol(e.get("kinase_gene", ""))
        if k and key:
            agent_by_kinase[k].add(key)

    per_kinase = {}
    for kinase in sorted(gold["kinase_names"]):
        gold_keys = {
            make_triplet_key(e["kinase_gene"], e["substrate_gene"], e["phospho_site"])
            for e in gold["by_kinase"].get(kinase, [])
        }
        agent_keys = agent_by_kinase.get(kinase, set())
        tp = len(gold_keys & agent_keys)
        fp = len(agent_keys - gold_keys)
        fn = len(gold_keys - agent_keys)
        prec = round(tp / (tp + fp), 4) if (tp + fp) > 0 else 0
        rec = round(tp / (tp + fn), 4) if (tp + fn) > 0 else 0
        f1 = round(2 * prec * rec / (prec + rec), 4) if (prec + rec) > 0 else 0

        per_kinase[kinase] = {
            "gold_entries": len(gold_keys),
            "agent_entries": len(agent_keys),
            "tp": tp, "fp": fp, "fn": fn,
            "precision": prec, "recall": rec, "f1": f1,
            "discovered": kinase in agent_by_kinase,
        }

    return per_kinase


def score_per_tier(agent_entries: list, gold: dict) -> dict:
    """Score recall by kinase tier (A: 100+, B: 20-99, C: 5-19, D: <5 substrates)."""
    per_kinase = score_per_kinase(agent_entries, gold)

    tiers = {"A": [], "B": [], "C": [], "D": []}
    for kinase, pk in per_kinase.items():
        n = pk["gold_entries"]
        if n >= 100:
            tiers["A"].append(pk)
        elif n >= 20:
            tiers["B"].append(pk)
        elif n >= 5:
            tiers["C"].append(pk)
        else:
            tiers["D"].append(pk)

    tier_scores = {}
    for tier, entries in tiers.items():
        if not entries:
            tier_scores[tier] = {"kinases": 0, "gold_entries": 0, "recall": 0}
            continue
        total_gold = sum(e["gold_entries"] for e in entries)
        total_tp = sum(e["tp"] for e in entries)
        tier_scores[tier] = {
            "kinases": len(entries),
            "gold_entries": total_gold,
            "recall": round(total_tp / total_gold, 4) if total_gold else 0,
        }

    return tier_scores


def score_atlas(agent_entries: list, gold: dict) -> dict:
    """Run all scoring metrics and return a comprehensive result."""
    al = score_triplets(agent_entries, gold)
    cl = score_columns(agent_entries, gold)
    kd = score_kinase_discovery(agent_entries, gold)
    cr = score_cross_referencing(agent_entries)
    pt = score_per_tier(agent_entries, gold)

    overview = {
        "atlas_size": al["total_agent_entries"],
        "recall": al["recall"],
        "precision": al["precision"],
        "f1": al["f1"],
        "kinases_found": f"{kd['kinases_discovered']}/{kd['kinases_in_gold']}",
        "multi_db_pct": cr["multi_db_pct"],
        "peptide_accuracy": cl["peptide_accuracy"],  # case-insensitive (primary)
    }

    return {
        "overview": overview,
        "atlas_level": al,
        "column_level": cl,
        "kinase_discovery": kd,
        "cross_referencing": cr,
        "per_tier": pt,
    }


def main():
    parser = argparse.ArgumentParser(description="Score agent atlas against PA2.0 gold standard")
    parser.add_argument("--atlas", required=True, help="Path to agent atlas JSON")
    parser.add_argument("--gold", required=True, help="Path to gold standard JSON")
    parser.add_argument("--output", required=True, help="Output directory for score files")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading gold standard: {args.gold}")
    gold = load_gold(args.gold)
    print(f"  {gold['metadata']['total_entries']} entries, {len(gold['kinase_names'])} kinases")

    print(f"Loading atlas: {args.atlas}")
    atlas = load_atlas(args.atlas)
    print(f"  {len(atlas)} entries")

    print("Scoring...")
    scores = score_atlas(atlas, gold)

    # Print summary
    ov = scores["overview"]
    al = scores["atlas_level"]
    cl = scores["column_level"]
    print(f"\n{'='*60}")
    print(f"RESULTS")
    print(f"{'='*60}")
    print(f"Atlas size:       {ov['atlas_size']}")
    print(f"Recall:           {ov['recall']}")
    print(f"Precision:        {ov['precision']}")
    print(f"F1:               {ov['f1']}")
    print(f"Kinases found:    {ov['kinases_found']}")
    print(f"Multi-DB:         {ov['multi_db_pct']}%")
    print(f"Peptide accuracy: {ov['peptide_accuracy']} (case-insensitive)")
    print(f"  Peptide exact (case-sensitive): {cl['peptide_exact_accuracy']}")
    print(f"  Peptide mismatches: {cl['peptide_mismatch']}/{cl['peptide_total']}")
    print(f"  TP={al['true_positives']} FP={al['false_positives']} FN={al['false_negatives']}")
    for tier, ts in scores["per_tier"].items():
        print(f"  Tier {tier}: {ts['kinases']} kinases, recall={ts['recall']}")

    # Save
    # Summary (without per-kinase and mismatch details)
    summary = {k: v for k, v in scores.items()}
    summary["column_level"] = {k: v for k, v in cl.items() if k != "peptide_mismatches"}

    with open(output_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    # Per-kinase details
    per_kinase = score_per_kinase(atlas, gold)
    with open(output_dir / "per_kinase.json", "w") as f:
        json.dump(per_kinase, f, indent=2)

    # Peptide mismatches
    with open(output_dir / "peptide_mismatches.json", "w") as f:
        json.dump(cl["peptide_mismatches"], f, indent=2)

    print(f"\nScores saved to: {output_dir}/")


if __name__ == "__main__":
    main()
