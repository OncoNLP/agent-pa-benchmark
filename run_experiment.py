#!/usr/bin/env python3
"""
Entry point for the PhosphoAtlas Agent Benchmark.

Usage:
  # Step 1: Parse gold standard (run once)
  python run_experiment.py --step parse --pa2-input gold_standard/input/PA2.xlsx

  # Step 2: Run an agent experiment
  python run_experiment.py --step run --model claude-opus --condition naive

  # Step 3: Score results
  python run_experiment.py --step score --atlas results/raw/claude-opus_naive_run0.json

  # Step 4: Compare across models
  python run_experiment.py --step compare --scores-dir results/scores/

  # Run all steps for a model
  python run_experiment.py --step all --model claude-opus --condition naive --pa2-input gold_standard/input/PA2.xlsx
"""
import argparse
import json
import sys
from pathlib import Path


def step_parse(args):
    """Parse PA2.0 XLSX into gold standard JSON."""
    from gold_standard.parse_pa2 import main as parse_main
    sys.argv = [
        "parse_pa2",
        "--input", args.pa2_input,
        "--output", "gold_standard/parsed/phosphoatlas_gold.json",
    ]
    if args.pa2_only:
        sys.argv.append("--pa2-only")
    parse_main()


def step_score(args):
    """Score an atlas against the gold standard."""
    from evaluation.scorer import main as score_main
    atlas_path = args.atlas
    # Derive output dir from atlas filename
    stem = Path(atlas_path).stem
    output_dir = f"results/scores/{stem}"
    sys.argv = [
        "scorer",
        "--atlas", atlas_path,
        "--gold", "gold_standard/parsed/phosphoatlas_gold.json",
        "--output", output_dir,
    ]
    score_main()


def step_compare(args):
    """Compare scores across multiple runs."""
    from evaluation.analyzer import compare_runs
    compare_runs(args.scores_dir, args.output or "results/summaries/comparison.json")


def main():
    parser = argparse.ArgumentParser(
        description="PhosphoAtlas Agent Benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--step",
        choices=["parse", "run", "score", "compare", "all"],
        required=True,
        help="Which step to execute",
    )
    parser.add_argument("--model", help="Model name (e.g., claude-opus, gpt-5)")
    parser.add_argument("--condition", default="naive", help="Experiment condition")
    parser.add_argument("--pa2-input", help="Path to PA2 XLSX file")
    parser.add_argument("--pa2-only", action="store_true", help="Filter to PA2 entries only")
    parser.add_argument("--atlas", help="Path to atlas JSON (for scoring)")
    parser.add_argument("--scores-dir", default="results/scores/", help="Scores directory (for comparison)")
    parser.add_argument("--output", help="Output path")
    parser.add_argument("--run-id", type=int, default=0, help="Run number (for repeated runs)")
    args = parser.parse_args()

    if args.step == "parse":
        if not args.pa2_input:
            parser.error("--pa2-input required for parse step")
        step_parse(args)
    elif args.step == "score":
        if not args.atlas:
            parser.error("--atlas required for score step")
        step_score(args)
    elif args.step == "compare":
        step_compare(args)
    elif args.step == "run":
        print("To run an agent experiment, implement your agent runner in agents/")
        print("See agents/base_agent.py for the abstract interface.")
        print(f"Model: {args.model}, Condition: {args.condition}")
    elif args.step == "all":
        if not args.pa2_input:
            parser.error("--pa2-input required for all step")
        step_parse(args)
        print("\nGold standard parsed. Implement your agent runner to continue.")


if __name__ == "__main__":
    main()
