"""
Runner for the paper-informed prompt experiment.

The prompt lives in paper_informed_prompt.txt. To plug in the actual paper:
  1. Open paper_informed_prompt.txt
  2. Replace the content between [PLUG-IN SECTION] markers with the
     methodology text from Olow et al. 2016 and its supplement
  3. Re-run this script — no code changes needed

Usage
-----
  # From project root:
  python contributions/andrew_qwen3_235b/qwen_prompt_testing/run_paper_informed.py

  # Smoke test:
  python contributions/andrew_qwen3_235b/qwen_prompt_testing/run_paper_informed.py --max-tool-calls 20

  # Full run + score:
  python contributions/andrew_qwen3_235b/qwen_prompt_testing/run_paper_informed.py --score

Output
------
  contributions/andrew_qwen3_235b/results/paper_informed/atlas.json
  contributions/andrew_qwen3_235b/results/paper_informed/run_log.json
  contributions/andrew_qwen3_235b/results/paper_informed/scores/
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from contributions.andrew_qwen3_235b.qwen_prompt_testing.agent_with_http import (
    QwenAgentWithHTTP,
)

PROMPT_FILE = Path(__file__).resolve().parent / "paper_informed_prompt.txt"
OUTPUT_DIR  = Path(__file__).resolve().parent.parent / "results" / "paper_informed"
ATLAS_PATH  = OUTPUT_DIR / "atlas.json"
LOG_PATH    = OUTPUT_DIR / "run_log.json"
GOLD_PATH   = PROJECT_ROOT / "gold_standard" / "parsed" / "phosphoatlas_gold.json"
DATABASES_DIR = PROJECT_ROOT / "databases"


def load_prompt() -> str:
    if not PROMPT_FILE.exists():
        raise FileNotFoundError(f"Prompt file not found: {PROMPT_FILE}")
    return PROMPT_FILE.read_text().strip()


def run_experiment(max_tool_calls: int = 5000):
    prompt = load_prompt()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    agent = QwenAgentWithHTTP(
        databases_dir=str(DATABASES_DIR),
        max_tool_calls=max_tool_calls,
    )

    print(f"[RUN] model={agent.MODEL_ID}")
    print(f"[RUN] condition=paper_informed  max_tool_calls={max_tool_calls}")
    print(f"[RUN] prompt={PROMPT_FILE.name}  ({len(prompt)} chars)")

    result = agent.run(prompt, condition="paper_informed")

    with open(ATLAS_PATH, "w") as f:
        json.dump(result["atlas"], f, indent=2)
    print(f"[SAVED] {len(result['atlas'])} entries → {ATLAS_PATH}")

    log = {k: v for k, v in result.items() if k != "atlas"}
    with open(LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)
    print(f"[SAVED] run log → {LOG_PATH}")

    return result


def score_atlas():
    if not ATLAS_PATH.exists():
        print("[ERROR] atlas not found — run experiment first.")
        return
    if not GOLD_PATH.exists():
        print(f"[ERROR] gold standard not found at {GOLD_PATH}")
        return

    scores_dir = OUTPUT_DIR / "scores"
    scores_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "evaluation.scorer",
        "--atlas", str(ATLAS_PATH),
        "--gold",  str(GOLD_PATH),
        "--output", str(scores_dir),
    ]
    print("[SCORE] Running scorer...")
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)
    print(f"[SAVED] Scores → {scores_dir}/")


def main():
    parser = argparse.ArgumentParser(
        description="Run paper-informed prompt experiment for Qwen3-235B"
    )
    parser.add_argument(
        "--max-tool-calls",
        type=int,
        default=5000,
        help="Cap tool calls for smoke testing (e.g. --max-tool-calls 20). Default: 5000",
    )
    parser.add_argument(
        "--score",
        action="store_true",
        help="Score the atlas against the gold standard after running",
    )
    args = parser.parse_args()

    run_experiment(max_tool_calls=args.max_tool_calls)

    if args.score:
        score_atlas()


if __name__ == "__main__":
    main()
