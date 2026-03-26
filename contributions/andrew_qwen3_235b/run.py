"""
Run script for the Qwen3_235b-Instruct PhosphoAtlas agent.

Usage
-----
  # From the project root:
  python contributions/andrew_qwen3_235b/run.py --condition naive

  # Available conditions (must match config.yaml):
  #   naive | paper_informed | pipeline_guided | knowledge_only

  # Dry-run (verifies API key + tool connectivity, no full run):
  python contributions/andrew_qwen3_235b/run.py --dry-run

Requirements
------------
  pip install openai
  export TOGETHER_API_KEY="your-key-here"

Output
------
  contributions/andrew_qwen3_235b/atlas.json        raw atlas entries
  contributions/andrew_qwen3_235b/run_log.json      timing + tool call metrics
  contributions/andrew_qwen3_235b/scores/           scored after run (if --score flag set)
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Resolve project root regardless of working directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

CONTRIBUTION_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = PROJECT_ROOT / "agents" / "prompts"
DATABASES_DIR = PROJECT_ROOT / "databases"
GOLD_PATH = PROJECT_ROOT / "gold_standard" / "parsed" / "phosphoatlas_gold.json"


def load_prompt(condition: str) -> str:
    prompt_file = PROMPTS_DIR / f"{condition}.txt"
    if not prompt_file.exists():
        raise FileNotFoundError(
            f"Prompt file not found: {prompt_file}\n"
            f"Available conditions: {[p.stem for p in PROMPTS_DIR.glob('*.txt')]}"
        )
    return prompt_file.read_text()


def run_agent(condition: str, dry_run: bool = False, max_tool_calls: int = 5000):
    from contributions.andrew_qwen3_235b.agent_runner import QwenAgent

    agent = QwenAgent(databases_dir=str(DATABASES_DIR), max_tool_calls=max_tool_calls)

    if dry_run:
        print("[DRY RUN] Agent initialised. Testing list_databases tool call...")
        result = agent.tools.dispatch("list_databases", {})
        print(f"[DRY RUN] list_databases returned: {json.dumps(result, indent=2)}")
        print("[DRY RUN] API key and tool connectivity OK.")
        return

    prompt = load_prompt(condition)
    print(f"[RUN] condition={condition}  model={agent.model_name}")

    if condition == "knowledge_only":
        result = agent.run_knowledge_only(prompt)
    else:
        result = agent.run(prompt, condition=condition)

    # Save atlas
    atlas_path = CONTRIBUTION_DIR / "atlas.json"
    with open(atlas_path, "w") as f:
        json.dump(result["atlas"], f, indent=2)
    print(f"[SAVED] {len(result['atlas'])} entries → {atlas_path}")

    # Save run log (everything except the full atlas to keep it readable)
    log = {k: v for k, v in result.items() if k != "atlas"}
    log_path = CONTRIBUTION_DIR / "run_log.json"
    with open(log_path, "w") as f:
        json.dump(log, f, indent=2)
    print(f"[SAVED] run log → {log_path}")

    return result


def score_atlas():
    """Run the scorer against the saved atlas.json."""
    atlas_path = CONTRIBUTION_DIR / "atlas.json"
    scores_dir = CONTRIBUTION_DIR / "scores"
    scores_dir.mkdir(exist_ok=True)

    if not atlas_path.exists():
        print("[ERROR] atlas.json not found. Run the agent first.")
        return

    if not GOLD_PATH.exists():
        print(f"[ERROR] Gold standard not found at {GOLD_PATH}")
        return

    cmd = [
        sys.executable, "-m", "evaluation.scorer",
        "--atlas", str(atlas_path),
        "--gold", str(GOLD_PATH),
        "--output", str(scores_dir),
    ]
    print(f"[SCORE] Running scorer...")
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)
    print(f"[SAVED] Scores → {scores_dir}/")


def main():
    parser = argparse.ArgumentParser(description="Run Qwen3-235B PhosphoAtlas agent")
    parser.add_argument(
        "--condition",
        default="naive",
        choices=["naive", "paper_informed", "pipeline_guided", "knowledge_only"],
        help="Experimental condition (default: naive)",
    )
    parser.add_argument(
        "--score",
        action="store_true",
        help="Score the atlas against the gold standard after running",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify API key and tool connectivity without running a full experiment",
    )
    parser.add_argument(
        "--max-tool-calls",
        type=int,
        default=5000,
        help="Cap tool calls for smoke testing (e.g. --max-tool-calls 20). Default: 5000",
    )
    args = parser.parse_args()

    run_agent(condition=args.condition, dry_run=args.dry_run, max_tool_calls=args.max_tool_calls)

    if args.score and not args.dry_run:
        score_atlas()


if __name__ == "__main__":
    main()
