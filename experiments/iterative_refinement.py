"""
Iterative Refinement Experiment for PhosphoAtlas Agent Benchmark.

Design: Give the agent its scoring results after each round, then let it
try again with that feedback. Measures self-improvement capability.

Usage:
    # Round 1 (same as naive — baseline)
    python experiments/iterative_refinement.py --round 1

    # Round 2 (agent receives Round 1 scores as feedback)
    python experiments/iterative_refinement.py --round 2

    # Round 3 (agent receives Round 1 + Round 2 scores)
    python experiments/iterative_refinement.py --round 3

    # Run all 3 rounds sequentially
    python experiments/iterative_refinement.py --all

Requires: ANTHROPIC_API_KEY or Claude Code Max subscription
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GOLD_STANDARD = PROJECT_ROOT / "gold_standard" / "parsed" / "phosphoatlas_gold.json"
RESULTS_DIR = PROJECT_ROOT / "contributions" / "claude_opus_iterative"
NAIVE_PROMPT = (PROJECT_ROOT / "agents" / "prompts" / "naive.txt").read_text()

# Tools available to the agent (same as naive run)
TOOLS_DESCRIPTION = """
You have access to the following tools to query phosphorylation databases:

1. web_search(query) - Search the web for database URLs and documentation
2. web_fetch(url) - Fetch content from a URL (API endpoint, download link, etc.)
3. submit_atlas(entries) - Submit your completed atlas as a JSON array

Each entry in the atlas should have:
- kinase_gene: string (kinase gene symbol)
- substrate_gene: string (substrate gene symbol)
- phospho_site: string (e.g., "S807", "Y15")
- heptameric_peptide: string (optional, 15-char peptide around site)
- substrate_uniprot: string (optional, UniProt accession)
- supporting_databases: list of strings (which databases support this entry)
"""

FEEDBACK_TEMPLATE = """
=== PERFORMANCE FEEDBACK FROM ROUND {round_num} ===

Your atlas from Round {round_num} was scored against a curated gold standard.
Here are your results:

TRIPLET-LEVEL METRICS:
- Entries submitted: {atlas_size}
- True Positives (correct): {tp}
- False Positives (not in gold): {fp}
- False Negatives (missed): {fn}
- Precision: {precision}
- Recall: {recall}
- F1: {f1}

KINASE DISCOVERY:
- Kinases found: {kinases_found}
- Kinases missed: {kinases_missed}
- Discovery rate: {discovery_rate}

COLUMN-LEVEL ACCURACY (for matched entries):
- Phospho-site accuracy: {site_accuracy}
- Peptide exact accuracy: {peptide_accuracy}
- Peptide close accuracy (case-insensitive): {peptide_close_accuracy}
- UniProt accuracy: {uniprot_accuracy}

DATABASE USAGE:
- Databases accessed: {databases}
- Multi-DB coverage: {multi_db_pct}%

TOP 10 MISSED KINASES (by # of gold entries):
{missed_kinases_detail}

AREAS FOR IMPROVEMENT:
{improvement_suggestions}

=== END FEEDBACK ===

Based on this feedback, please try again. Your goal is to IMPROVE your recall
(find more of the {fn} entries you missed) while maintaining accuracy.
Focus especially on:
1. The missed kinases listed above
2. Databases you did not query in Round {round_num}
3. Any systematic gaps in your coverage
"""


def generate_feedback(summary_path, round_num):
    """Generate human-readable feedback from scoring results."""
    with open(summary_path) as f:
        s = json.load(f)

    al = s["atlas_level"]
    cl = s["column_level"]
    kd = s["kinase_discovery"]
    cr = s["cross_referencing"]

    # Top missed kinases by gold entry count
    missed = kd.get("missed_kinases", [])[:10]
    missed_detail = "\n".join(f"  - {k}" for k in missed) if missed else "  (none)"

    # Generate improvement suggestions
    suggestions = []
    if al["recall"] < 0.95:
        suggestions.append(
            f"- Your recall is {al['recall']:.1%}. You missed {al['false_negatives']} entries. "
            f"Try querying additional databases beyond {', '.join(cr.get('db_coverage', {}).keys())}."
        )
    if cr.get("multi_db_pct", 0) < 20:
        suggestions.append(
            f"- Only {cr.get('multi_db_pct', 0)}% of your entries are cross-referenced across "
            f"multiple databases. Try SIGNOR, UniProt, and PhosphoSitePlus for better coverage."
        )
    if cl.get("peptide_exact_accuracy", 0) < 0.9:
        suggestions.append(
            f"- Peptide accuracy is {cl.get('peptide_exact_accuracy', 0):.1%}. "
            f"Ensure you retrieve the SITE_+/-7_AA field from each database."
        )
    if len(kd.get("missed_kinases", [])) > 10:
        suggestions.append(
            f"- You missed {len(kd['missed_kinases'])} kinases. Some may be in databases "
            f"you didn't query, or listed under alternative gene symbols."
        )

    return FEEDBACK_TEMPLATE.format(
        round_num=round_num,
        atlas_size=al["total_agent_entries"],
        tp=al["true_positives"],
        fp=al["false_positives"],
        fn=al["false_negatives"],
        precision=f"{al['precision']:.4f}",
        recall=f"{al['recall']:.4f}",
        f1=f"{al['f1']:.4f}",
        kinases_found=kd.get("kinases_discovered", "?"),
        kinases_missed=kd.get("kinases_missed", "?"),
        discovery_rate=f"{kd.get('discovery_rate', 0):.1%}",
        site_accuracy=f"{cl.get('site_accuracy', 0):.1%}",
        peptide_accuracy=f"{cl.get('peptide_exact_accuracy', 0):.1%}",
        peptide_close_accuracy=f"{cl.get('peptide_close_accuracy', 0):.1%}",
        uniprot_accuracy=f"{cl.get('uniprot_accuracy', 0):.1%}",
        databases=", ".join(cr.get("db_coverage", {}).keys()),
        multi_db_pct=cr.get("multi_db_pct", 0),
        missed_kinases_detail=missed_detail,
        improvement_suggestions="\n".join(suggestions) if suggestions else "- Good job! Try to maintain accuracy while expanding coverage.",
    )


def score_atlas(atlas_path, output_dir):
    """Run the scorer on an atlas file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "evaluation.scorer",
        "--atlas", str(atlas_path),
        "--gold", str(GOLD_STANDARD),
        "--output", str(output_dir),
    ]
    print(f"[SCORING] {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        print(f"[ERROR] Scorer failed:\n{result.stderr}")
        return False
    print(f"[SCORING] Done. Results in {output_dir}")
    return True


def build_prompt_for_round(round_num):
    """Build the full prompt for a given round."""
    parts = [NAIVE_PROMPT, "", TOOLS_DESCRIPTION]

    # Add feedback from all previous rounds
    for prev_round in range(1, round_num):
        summary_path = RESULTS_DIR / f"round{prev_round}" / "scores" / "summary.json"
        if summary_path.exists():
            feedback = generate_feedback(summary_path, prev_round)
            parts.append(feedback)
        else:
            print(f"[WARN] No scores found for round {prev_round} at {summary_path}")

    return "\n\n".join(parts)


def run_round_claude_code(round_num):
    """
    Run one round using Claude Code as the agent.

    This spawns a Claude Code subprocess with the prompt,
    letting it act autonomously to build the atlas.
    """
    round_dir = RESULTS_DIR / f"round{round_num}"
    round_dir.mkdir(parents=True, exist_ok=True)
    atlas_path = round_dir / "atlas.json"
    scores_dir = round_dir / "scores"

    if atlas_path.exists():
        print(f"[ROUND {round_num}] Atlas already exists at {atlas_path}. Skipping curation.")
        print(f"[ROUND {round_num}] Re-scoring...")
        score_atlas(atlas_path, scores_dir)
        return

    prompt = build_prompt_for_round(round_num)

    # Save the prompt for reproducibility
    prompt_path = round_dir / "prompt_used.txt"
    prompt_path.write_text(prompt)
    print(f"[ROUND {round_num}] Prompt saved to {prompt_path}")
    print(f"[ROUND {round_num}] Prompt length: {len(prompt)} chars")

    # Instructions for manual execution
    print(f"\n{'='*60}")
    print(f"ROUND {round_num} READY")
    print(f"{'='*60}")
    print(f"The prompt has been saved to:\n  {prompt_path}")
    print(f"\nTo run this round with Claude Code:")
    print(f"  1. Open a new Claude Code session")
    print(f"  2. Paste the content of {prompt_path}")
    print(f"  3. Let the agent build the atlas")
    print(f"  4. Save the atlas JSON to:\n     {atlas_path}")
    print(f"  5. Then re-run this script with --round {round_num} to score it")
    print(f"{'='*60}\n")

    # Alternative: run via Claude Code CLI if available
    claude_path = os.popen("which claude").read().strip()
    if claude_path:
        print(f"[ROUND {round_num}] Claude Code CLI found at {claude_path}")
        print(f"[ROUND {round_num}] Attempting automated run...")

        # Build the task prompt
        task = (
            f"{prompt}\n\n"
            f"IMPORTANT: Save your final atlas as a JSON array to:\n"
            f"  {atlas_path}\n\n"
            f"The JSON should be an array of objects with fields: "
            f"kinase_gene, substrate_gene, phospho_site, "
            f"heptameric_peptide, substrate_uniprot, supporting_databases.\n\n"
            f"When finished, write the file and confirm completion."
        )

        # Run claude CLI in non-interactive mode
        cmd = [
            claude_path, "--print",
            "--allowedTools", "Bash,Read,Write,WebFetch,WebSearch",
            "-p", task,
        ]

        print(f"[ROUND {round_num}] Starting Claude Code agent...")
        t0 = time.time()
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800,  # 30 minute timeout
            cwd=str(PROJECT_ROOT),
        )
        elapsed = time.time() - t0

        # Save run log
        run_log = {
            "agent": "Claude Opus 4.6 (iterative)",
            "round": round_num,
            "condition": f"iterative_round{round_num}",
            "elapsed_seconds": round(elapsed, 1),
            "exit_code": result.returncode,
            "stdout_length": len(result.stdout),
            "stderr_length": len(result.stderr),
        }

        (round_dir / "run_log.json").write_text(json.dumps(run_log, indent=2))
        (round_dir / "claude_output.txt").write_text(result.stdout[-5000:])  # last 5k chars

        if atlas_path.exists():
            print(f"[ROUND {round_num}] Atlas created! Size: {atlas_path.stat().st_size} bytes")
            print(f"[ROUND {round_num}] Elapsed: {elapsed:.1f}s")
            score_atlas(atlas_path, scores_dir)
        else:
            print(f"[ROUND {round_num}] Atlas NOT created. Check {round_dir}/claude_output.txt")
            print(f"[ROUND {round_num}] You may need to manually save the atlas.")
    else:
        print("[INFO] Claude Code CLI not found. Run manually per instructions above.")


def compare_rounds():
    """Compare results across all rounds."""
    rounds = []
    for round_dir in sorted(RESULTS_DIR.glob("round*")):
        summary = round_dir / "scores" / "summary.json"
        if summary.exists():
            with open(summary) as f:
                s = json.load(f)
            al = s["atlas_level"]
            cl = s["column_level"]
            kd = s["kinase_discovery"]
            rounds.append({
                "round": round_dir.name,
                "entries": al["total_agent_entries"],
                "recall": al["recall"],
                "precision": al["precision"],
                "f1": al["f1"],
                "kinases": kd["kinases_discovered"],
                "fn": al["false_negatives"],
                "peptide": cl.get("peptide_exact_accuracy", 0),
            })

    if not rounds:
        print("No completed rounds found.")
        return

    print(f"\n{'='*80}")
    print("ITERATIVE REFINEMENT COMPARISON")
    print(f"{'='*80}")
    print(f"{'Round':<10} {'Entries':>8} {'Recall':>8} {'Prec':>8} {'F1':>8} {'Kinases':>8} {'FN':>6} {'Pep Acc':>8}")
    print("-" * 80)
    for r in rounds:
        print(f"{r['round']:<10} {r['entries']:>8} {r['recall']:>8.4f} {r['precision']:>8.4f} "
              f"{r['f1']:>8.4f} {r['kinases']:>8} {r['fn']:>6} {r['peptide']:>8.1%}")

    if len(rounds) > 1:
        r1, rl = rounds[0], rounds[-1]
        print(f"\nImprovement (round1 -> {rl['round']}):")
        print(f"  Recall: {r1['recall']:.4f} -> {rl['recall']:.4f} ({rl['recall']-r1['recall']:+.4f})")
        print(f"  F1:     {r1['f1']:.4f} -> {rl['f1']:.4f} ({rl['f1']-r1['f1']:+.4f})")
        print(f"  FN:     {r1['fn']} -> {rl['fn']} ({rl['fn']-r1['fn']:+d})")

    # Save comparison
    comp_path = RESULTS_DIR / "iterative_comparison.json"
    with open(comp_path, "w") as f:
        json.dump(rounds, f, indent=2)
    print(f"\nSaved to {comp_path}")


def main():
    parser = argparse.ArgumentParser(description="Iterative Refinement Experiment")
    parser.add_argument("--round", type=int, help="Run a specific round (1, 2, or 3)")
    parser.add_argument("--all", action="store_true", help="Run all 3 rounds sequentially")
    parser.add_argument("--compare", action="store_true", help="Compare results across rounds")
    parser.add_argument("--feedback", type=int, help="Generate and print feedback for round N")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.feedback:
        summary = RESULTS_DIR / f"round{args.feedback}" / "scores" / "summary.json"
        if summary.exists():
            print(generate_feedback(summary, args.feedback))
        else:
            print(f"No scores for round {args.feedback}. Run that round first.")
        return

    if args.compare:
        compare_rounds()
        return

    if args.all:
        for r in range(1, 4):
            print(f"\n{'#'*60}")
            print(f"# ROUND {r}")
            print(f"{'#'*60}")
            run_round_claude_code(r)
            # Check if atlas was created before proceeding
            atlas = RESULTS_DIR / f"round{r}" / "atlas.json"
            if not atlas.exists():
                print(f"[STOP] Round {r} atlas not created. Fix before continuing.")
                break
        compare_rounds()
        return

    if args.round:
        run_round_claude_code(args.round)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
