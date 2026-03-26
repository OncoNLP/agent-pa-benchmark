"""
Runner for the URL-injection experiment.

Usage
-----
  # From project root:
  python contributions/andrew_qwen3_235b/qwen_prompt_testing/test_qwen_prompt.py

  # Smoke test (cap tool calls):
  python contributions/andrew_qwen3_235b/qwen_prompt_testing/test_qwen_prompt.py --max-tool-calls 20

  # Score after run:
  python contributions/andrew_qwen3_235b/qwen_prompt_testing/test_qwen_prompt.py --score

Requirements
------------
  pip install openai requests
  export TOGETHER_API_KEY="your-key-here"

Output
------
  contributions/andrew_qwen3_235b/qwen_prompt_testing/explicit_prompt_test_atlas.json
  contributions/andrew_qwen3_235b/qwen_prompt_testing/explicit_prompt_test_log.json
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
    EXPLICIT_PROMPT,
)

OUTPUT_DIR  = Path(__file__).resolve().parent
ATLAS_PATH  = OUTPUT_DIR / "explicit_prompt_test_atlas.json"
LOG_PATH    = OUTPUT_DIR / "explicit_prompt_test_log.json"
GOLD_PATH   = PROJECT_ROOT / "gold_standard" / "parsed" / "phosphoatlas_gold.json"
DATABASES_DIR = PROJECT_ROOT / "databases"


def run_experiment(max_tool_calls: int = 5000):
    agent = QwenAgentWithHTTP(
        databases_dir=str(DATABASES_DIR),
        max_tool_calls=max_tool_calls,
    )

    print(f"[RUN] model={agent.MODEL_ID}")
    print(f"[RUN] condition=explicit_prompt  max_tool_calls={max_tool_calls}")

    result = agent.run(EXPLICIT_PROMPT, condition="explicit_prompt")

    # Save atlas
    with open(ATLAS_PATH, "w") as f:
        json.dump(result["atlas"], f, indent=2)
    print(f"[SAVED] {len(result['atlas'])} entries → {ATLAS_PATH}")

    # Save log (drop full atlas to keep it readable)
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
    scores_dir.mkdir(exist_ok=True)

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
        description="Run URL-injection experiment for Qwen3-235B"
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
