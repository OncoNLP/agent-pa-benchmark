#!/usr/bin/env python3
"""
Abstract base agent for the PhosphoAtlas Benchmark.

All model-specific agents inherit from this class. It handles:
  - Tool call loop (call model → execute tools → feed back results → repeat)
  - Budget enforcement (max tool calls, timeout)
  - Logging (every tool call, every model response, timing)
  - Atlas submission (the agent calls submit_atlas when done)

Subclasses implement only:
  - _call_model(messages, tools) → response with tool calls or text
  - _parse_tool_calls(response) → list of (tool_name, arguments) tuples
  - _parse_text(response) → str (for final text responses)
  - _is_done(response) → bool (whether the model signals completion)
"""
import json
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from databases.tools import DatabaseTools


class BaseAgent(ABC):
    """Abstract base for all benchmark agents."""

    def __init__(
        self,
        model_name: str,
        databases_dir: str = "databases",
        max_tool_calls: int = 5000,
        timeout_minutes: int = 60,
    ):
        self.model_name = model_name
        self.tools = DatabaseTools(databases_dir)
        self.max_tool_calls = max_tool_calls
        self.timeout_minutes = timeout_minutes

        # State
        self.messages = []
        self.tool_call_count = 0
        self.start_time = None
        self.atlas = None  # set when agent submits
        self.strategy_summary = ""
        self.trace = []  # full conversation trace

        # Build tool definitions (add submit_atlas)
        self.tool_definitions = DatabaseTools.get_tool_definitions() + [
            {
                "type": "function",
                "function": {
                    "name": "submit_atlas",
                    "description": (
                        "Submit your completed phosphorylation atlas. "
                        "Call this when you have finished curating all "
                        "kinase-substrate-phosphosite relationships from all databases."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "entries": {
                                "type": "array",
                                "description": "Array of curated entries",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "kinase_gene": {"type": "string"},
                                        "substrate_gene": {"type": "string"},
                                        "phospho_site": {"type": "string"},
                                        "substrate_uniprot": {"type": "string"},
                                        "heptameric_peptide": {"type": "string"},
                                        "supporting_databases": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                    },
                                    "required": [
                                        "kinase_gene",
                                        "substrate_gene",
                                        "phospho_site",
                                    ],
                                },
                            },
                            "strategy_summary": {
                                "type": "string",
                                "description": "Brief summary of the curation strategy used",
                            },
                        },
                        "required": ["entries"],
                    },
                },
            }
        ]

    # === Abstract methods (subclasses implement) ===

    @abstractmethod
    def _call_model(self, messages: list, tools: list) -> dict:
        """Call the LLM and return its raw response."""

    @abstractmethod
    def _parse_tool_calls(self, response: dict) -> list[tuple[str, dict]]:
        """Extract tool calls from model response → [(tool_name, arguments), ...]"""

    @abstractmethod
    def _parse_text(self, response: dict) -> str:
        """Extract text content from model response."""

    @abstractmethod
    def _format_tool_result(self, tool_name: str, result: dict) -> dict:
        """Format a tool result as a message to feed back to the model."""

    # === Core loop ===

    def run(self, system_prompt: str, condition: str = "naive") -> dict:
        """Run the agent loop until it submits or hits budget limits.

        Returns a run result dict with atlas, metrics, and trace.
        """
        self.start_time = time.time()
        self.messages = [{"role": "system", "content": system_prompt}]
        self.trace = []

        self._log(f"[START] Model={self.model_name} Condition={condition}")
        self._log(f"[BUDGET] max_tool_calls={self.max_tool_calls} timeout={self.timeout_minutes}m")

        while not self._budget_exceeded():
            # Call model
            try:
                response = self._call_model(self.messages, self.tool_definitions)
            except Exception as e:
                self._log(f"[ERROR] Model call failed: {e}")
                self.trace.append({"type": "error", "error": str(e), "time": self._elapsed()})
                break

            # Parse tool calls
            tool_calls = self._parse_tool_calls(response)

            if not tool_calls:
                # Model returned text without tool calls — might be done
                text = self._parse_text(response)
                self._log(f"[TEXT] {text[:200]}...")
                self.trace.append({"type": "text", "content": text, "time": self._elapsed()})

                # Try to extract atlas from text (knowledge-only mode)
                if self.atlas is None and text:
                    self._try_extract_atlas_from_text(text)
                break

            # Execute each tool call
            for tool_name, arguments in tool_calls:
                self.tool_call_count += 1

                if tool_name == "submit_atlas":
                    self.atlas = arguments.get("entries", [])
                    self.strategy_summary = arguments.get("strategy_summary", "")
                    self._log(f"[SUBMIT] Atlas submitted: {len(self.atlas)} entries")
                    self.trace.append({
                        "type": "submit",
                        "entries": len(self.atlas),
                        "strategy": self.strategy_summary,
                        "time": self._elapsed(),
                    })
                    # Feed back confirmation
                    result = {
                        "status": "accepted",
                        "entries_received": len(self.atlas),
                        "message": "Atlas submitted successfully.",
                    }
                else:
                    # Execute database tool
                    result = self.tools.dispatch(tool_name, arguments)
                    self._log(
                        f"[TOOL {self.tool_call_count}] {tool_name}({json.dumps(arguments)}) "
                        f"-> {_truncate(json.dumps(result), 200)}"
                    )

                self.trace.append({
                    "type": "tool_call",
                    "call_number": self.tool_call_count,
                    "tool": tool_name,
                    "arguments": arguments,
                    "result_size": len(json.dumps(result)),
                    "time": self._elapsed(),
                })

                # Add tool result to messages
                tool_msg = self._format_tool_result(tool_name, result)
                self.messages.append(tool_msg)

                if self.atlas is not None:
                    break  # Atlas submitted, stop
                if self._budget_exceeded():
                    break

            if self.atlas is not None:
                break

        elapsed = self._elapsed()
        self._log(f"[DONE] {self.tool_call_count} tool calls, {elapsed:.1f}s, atlas={'yes' if self.atlas else 'no'}")

        return {
            "model": self.model_name,
            "condition": condition,
            "atlas": self.atlas or [],
            "strategy_summary": self.strategy_summary,
            "metrics": {
                "tool_calls": self.tool_call_count,
                "db_tool_calls": self.tools.call_count,
                "elapsed_seconds": round(elapsed, 1),
                "budget_exceeded": self._budget_exceeded(),
                "atlas_size": len(self.atlas) if self.atlas else 0,
            },
            "trace": self.trace,
            "tool_log": self.tools.call_log,
        }

    def run_knowledge_only(self, system_prompt: str) -> dict:
        """Special mode: no tools, just ask the model to list phospho relationships."""
        self.start_time = time.time()
        self.messages = [{"role": "system", "content": system_prompt}]

        self._log(f"[START] Knowledge-only mode, Model={self.model_name}")

        try:
            response = self._call_model(self.messages, tools=[])
            text = self._parse_text(response)
            self._try_extract_atlas_from_text(text)
        except Exception as e:
            self._log(f"[ERROR] {e}")

        elapsed = self._elapsed()
        return {
            "model": self.model_name,
            "condition": "knowledge_only",
            "atlas": self.atlas or [],
            "strategy_summary": "Pure LLM knowledge, no database tools",
            "metrics": {
                "tool_calls": 0,
                "db_tool_calls": 0,
                "elapsed_seconds": round(elapsed, 1),
                "atlas_size": len(self.atlas) if self.atlas else 0,
            },
            "trace": self.trace,
            "tool_log": [],
        }

    # === Iterative mode ===

    def run_iterative(self, system_prompt: str, scorer, gold, max_iterations: int = 3) -> dict:
        """Run naive first, then give recall feedback and let agent retry."""
        all_results = []

        for iteration in range(max_iterations):
            self._log(f"[ITER {iteration+1}/{max_iterations}]")

            if iteration == 0:
                result = self.run(system_prompt, condition=f"iterative_round{iteration+1}")
            else:
                # Generate feedback from previous round
                prev = all_results[-1]
                scores = scorer.score_atlas(prev["atlas"], gold)
                feedback = (
                    f"\n\nFEEDBACK from previous attempt:\n"
                    f"- You found {scores['atlas_level']['true_positives']} correct entries\n"
                    f"- You missed {scores['atlas_level']['false_negatives']} entries that exist in the databases\n"
                    f"- Your recall is {scores['atlas_level']['recall']}\n"
                    f"- Kinases discovered: {scores['kinase_discovery']['kinases_discovered']}/{scores['kinase_discovery']['kinases_in_gold']}\n"
                    f"\nPlease try again. Focus on finding the entries you missed. "
                    f"Make sure you paginate through ALL kinases in each database."
                )
                # Reset state
                self.tool_call_count = 0
                self.tools.reset_log()
                self.atlas = None
                result = self.run(system_prompt + feedback, condition=f"iterative_round{iteration+1}")

            all_results.append(result)

            if result.get("atlas"):
                self._log(f"  Round {iteration+1}: {len(result['atlas'])} entries")

        # Return the best result (highest atlas size, as a proxy)
        best = max(all_results, key=lambda r: len(r.get("atlas", [])))
        best["all_iterations"] = all_results
        best["condition"] = "iterative"
        return best

    # === Helpers ===

    def _budget_exceeded(self) -> bool:
        if self.tool_call_count >= self.max_tool_calls:
            self._log(f"[BUDGET] Tool call limit reached ({self.max_tool_calls})")
            return True
        if self._elapsed() > self.timeout_minutes * 60:
            self._log(f"[BUDGET] Timeout reached ({self.timeout_minutes}m)")
            return True
        return False

    def _elapsed(self) -> float:
        return time.time() - (self.start_time or time.time())

    def _log(self, msg: str):
        elapsed = self._elapsed()
        print(f"[{elapsed:7.1f}s] {msg}")

    def _try_extract_atlas_from_text(self, text: str):
        """Try to parse a JSON atlas from the model's text response."""
        # Look for JSON array in text
        import re
        matches = re.findall(r'\[[\s\S]*?\]', text)
        for match in matches:
            try:
                data = json.loads(match)
                if isinstance(data, list) and len(data) > 0:
                    # Validate it looks like atlas entries
                    first = data[0]
                    if "kinase_gene" in first or "kinase" in first:
                        self.atlas = data
                        self._log(f"[EXTRACT] Parsed {len(data)} entries from text")
                        return
            except json.JSONDecodeError:
                continue


def _truncate(s: str, n: int) -> str:
    return s[:n] + "..." if len(s) > n else s
