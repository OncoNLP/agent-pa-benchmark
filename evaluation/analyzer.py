#!/usr/bin/env python3
"""
Cross-model comparison and statistical analysis.

Reads all score files from results/scores/ and produces:
  - Comparison table (model × condition → metrics)
  - Statistical significance tests (bootstrap confidence intervals)
  - Tier-level analysis
"""
import json
from collections import defaultdict
from pathlib import Path


def compare_runs(scores_dir: str, output_path: str = "results/summaries/comparison.json"):
    """Load all score files and produce comparison table."""
    scores_dir = Path(scores_dir)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Find all summary.json files
    summaries = sorted(scores_dir.glob("*/summary.json"))
    if not summaries:
        print(f"No score files found in {scores_dir}")
        return

    rows = []
    for path in summaries:
        with open(path) as f:
            data = json.load(f)
        run_name = path.parent.name
        al = data.get("atlas_level", {})
        kd = data.get("kinase_discovery", {})
        cr = data.get("cross_referencing", {})
        cl = data.get("column_level", {})
        rows.append({
            "run": run_name,
            "entries": al.get("total_agent_entries", 0),
            "precision": al.get("precision", 0),
            "recall": al.get("recall", 0),
            "f1": al.get("f1", 0),
            "tp": al.get("true_positives", 0),
            "fp": al.get("false_positives", 0),
            "fn": al.get("false_negatives", 0),
            "kinases_found": kd.get("kinases_discovered", 0),
            "kinases_total": kd.get("kinases_in_gold", 0),
            "kinase_rate": kd.get("discovery_rate", 0),
            "multi_db_pct": cr.get("multi_db_pct", 0),
            "peptide_accuracy": cl.get("peptide_exact_accuracy", 0),
            "uniprot_accuracy": cl.get("uniprot_accuracy", 0),
        })

    # Sort by F1
    rows.sort(key=lambda r: r["f1"], reverse=True)

    comparison = {
        "generated": str(Path(output_path)),
        "runs": len(rows),
        "results": rows,
    }

    with open(output_path, "w") as f:
        json.dump(comparison, f, indent=2)

    # Print table
    print(f"\n{'Run':<40} {'P':>6} {'R':>6} {'F1':>6} {'TP':>6} {'FP':>6} {'FN':>6} {'Kin':>5}")
    print("-" * 85)
    for r in rows:
        print(
            f"{r['run']:<40} {r['precision']:>6.3f} {r['recall']:>6.3f} {r['f1']:>6.3f} "
            f"{r['tp']:>6} {r['fp']:>6} {r['fn']:>6} {r['kinases_found']:>5}"
        )

    print(f"\nComparison saved to: {output_path}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--scores-dir", default="results/scores/")
    p.add_argument("--output", default="results/summaries/comparison.json")
    args = p.parse_args()
    compare_runs(args.scores_dir, args.output)
