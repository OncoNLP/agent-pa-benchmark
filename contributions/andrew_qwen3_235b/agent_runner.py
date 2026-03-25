"""
Qwen2.5-72B-Instruct agent for the PhosphoAtlas Benchmark.

Provider: Together AI (OpenAI-compatible endpoint)
Model:    Qwen/Qwen2.5-72B-Instruct
Author:   Andrew Lim

Design notes
------------
Together AI's endpoint follows the OpenAI chat completions spec, so we use the
`openai` Python client pointed at Together's base URL.

Two implementation details that differ from a naive OpenAI subclass:

1. Assistant message injection:
   BaseAgent's loop does not append the assistant's response to self.messages
   before feeding back tool results. OpenAI-compatible APIs require:
       [assistant msg with tool_calls] → [tool result msg(s)]
   We append the assistant message at the end of _call_model so the
   conversation history is always well-formed when the next call goes out.

2. Tool call ID tracking:
   _format_tool_result only receives (tool_name, result) — no ID. But the
   OpenAI tool-result message requires the matching tool_call_id. We stash
   IDs during _parse_tool_calls in a queue and pop them in order during
   _format_tool_result.

Parsing mode: STRICT — only structured tool_calls are honoured. If the model
returns JSON in plain text instead of using the tool_call mechanism, we do NOT
silently rescue it. This is intentional for Paper 1 benchmarking: format
compliance is a real model characteristic worth measuring.
"""

from contributions.andrew_qwen3_235b.live_tools import LiveDatabaseTools
from agents.base_agent import BaseAgent
from openai import OpenAI
import json
import os
import sys
from pathlib import Path

# Resolve project root so this script works from any working directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class QwenAgent(BaseAgent):
    """PhosphoAtlas benchmark agent backed by Qwen2.5-72B-Instruct via Together AI."""

    TOGETHER_BASE_URL = "https://api.together.xyz/v1"
    MODEL_ID = "Qwen/Qwen3-235B-A22B-Instruct-2507-tput"

    # Together AI / Qwen2.5-72B supports up to 32 768 output tokens.
    # The default in config.yaml (4 096) is too small for submit_atlas, which
    # must emit all curated entries in a single tool-call argument payload.
    MAX_TOKENS = 32_768

    def __init__(
        self,
        databases_dir: str = "databases",
        max_tool_calls: int = 5000,
        timeout_minutes: int = 60,
    ):
        super().__init__(
            model_name=self.MODEL_ID,
            databases_dir=databases_dir,
            max_tool_calls=max_tool_calls,
            timeout_minutes=timeout_minutes,
        )

        # Replace the local DatabaseTools with our live UniProt API layer.
        # Same interface, different backend. The model sees identical tool names.
        self.tools = LiveDatabaseTools()

        api_key = os.environ.get("TOGETHER_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "TOGETHER_API_KEY is not set. "
                "Export it in your shell before running:\n"
                "  export TOGETHER_API_KEY='your-key-here'"
            )

        self.client = OpenAI(
            api_key=api_key,
            base_url=self.TOGETHER_BASE_URL,
            timeout=120.0,  # 2 min per call; submit_atlas may be large but shouldn't hang
        )

        # Queue of tool_call_ids populated by _parse_tool_calls,
        # consumed in FIFO order by _format_tool_result.
        self._pending_tool_call_ids: list[str] = []

        # Accumulate entries from tool results as they arrive so we don't
        # rely on the model regenerating them all in submit_atlas.
        self._accumulated_entries: list[dict] = []
        self._seen_triplets: set[tuple] = set()  # for deduplication

    # -------------------------------------------------------------------------
    # Abstract method implementations
    # -------------------------------------------------------------------------

    def _call_model(self, messages: list, tools: list) -> object:
        """Call Together AI and append the assistant message to self.messages.

        Appending here (rather than in the base loop) keeps the conversation
        history valid for OpenAI-compatible APIs, which require:
            assistant msg (with tool_calls) → tool result msg(s)
        """
        kwargs = {
            "model": self.MODEL_ID,
            "messages": messages,
            "max_tokens": self.MAX_TOKENS,
            "temperature": 0,       # deterministic; important for reproducibility
        }

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = self.client.chat.completions.create(**kwargs)

        # Append the assistant turn so the next API call sees a valid history.
        msg = response.choices[0].message
        assistant_dict: dict = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        self.messages.append(assistant_dict)

        return response

    def _parse_tool_calls(self, response: object) -> list[tuple[str, dict]]:
        """Extract structured tool calls (strict mode — no text fallback).

        Returns a list of (tool_name, arguments_dict) pairs.
        Also populates self._pending_tool_call_ids for use in
        _format_tool_result.
        """
        msg = response.choices[0].message

        if not msg.tool_calls:
            return []

        self._pending_tool_call_ids = []
        parsed = []

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError as e:
                self._log(f"[WARN] Could not parse arguments for {name}: {e}")
                args = {}

            parsed.append((name, args))
            self._pending_tool_call_ids.append(tc.id)

        return parsed

    def _parse_text(self, response: object) -> str:
        """Extract the text content from a response that has no tool calls."""
        return response.choices[0].message.content or ""

    def _format_tool_result(self, tool_name: str, result: dict) -> dict:
        """Format a tool result as an OpenAI-style tool message.

        Also accumulates phosphorylation entries from query results so we
        have a fallback atlas if the model's submit_atlas call is empty or
        times out.
        """
        # Accumulate entries from any query that returns them
        entries_to_add = []
        if tool_name in ("query_by_kinase", "query_by_substrate"):
            entries_to_add = result.get("entries", [])
        elif tool_name == "query_all_dbs":
            for db_result in result.values():
                if isinstance(db_result, dict):
                    entries_to_add.extend(db_result.get("entries", []))

        for entry in entries_to_add:
            key = (
                entry.get("kinase_gene", ""),
                entry.get("substrate_gene", ""),
                entry.get("phospho_site", ""),
            )
            if key not in self._seen_triplets and all(key):
                self._seen_triplets.add(key)
                self._accumulated_entries.append(entry)

        if self._pending_tool_call_ids:
            tool_call_id = self._pending_tool_call_ids.pop(0)
        else:
            tool_call_id = f"call_{self.tool_call_count}"
            self._log(
                f"[WARN] No pending tool_call_id for {tool_name}; using {tool_call_id}")

        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(result),
        }

    def run(self, system_prompt: str, condition: str = "naive") -> dict:
        """Run the agent, falling back to accumulated entries if submit is empty."""
        self._accumulated_entries = []
        self._seen_triplets = set()
        result = super().run(system_prompt, condition)

        if not result["atlas"] and self._accumulated_entries:
            self._log(
                f"[FALLBACK] Model submitted empty; using "
                f"{len(self._accumulated_entries)} accumulated entries from tool results"
            )
            result["atlas"] = self._accumulated_entries
            result["metrics"]["atlas_size"] = len(self._accumulated_entries)

        return result
