#!/usr/bin/env python3
"""
Unified Claude Agent Runner with Token Logging.

Runs Claude (Opus or Sonnet) as an autonomous agent that discovers and
queries phosphorylation databases to build a comprehensive atlas.

Token usage is captured from every API response and logged to run_log.json.

Usage:
    # Claude Opus naive
    python agents/claude_runner.py --model opus --condition naive \
        --output contributions/claude_opus_naive_v2

    # Claude Sonnet paper-informed
    python agents/claude_runner.py --model sonnet --condition paper_informed \
        --output contributions/claude_sonnet_paper_informed

    # Custom prompt
    python agents/claude_runner.py --model opus --prompt-file my_prompt.txt \
        --output contributions/my_run

Requires: ANTHROPIC_API_KEY environment variable.
"""
import argparse
import csv
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from databases.tools import DatabaseTools

try:
    import anthropic
except ImportError:
    sys.exit("Error: pip install anthropic")


MODEL_MAP = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

CONDITION_PROMPTS = {
    "naive": "agents/prompts/naive.txt",
    "paper_informed": "agents/prompts/paper_informed.txt",
    "pipeline_guided": "agents/prompts/pipeline_guided.txt",
}


# ═══════════════════════════════════════════════════════════════════════════════
# TOKEN TRACKER
# ═══════════════════════════════════════════════════════════════════════════════

class TokenTracker:
    """Track token usage and estimate cost across all API calls."""

    # Pricing per million tokens (USD) as of 2025-Q2
    PRICING = {
        "claude-opus-4-6": {
            "input": 15.0, "output": 75.0,
            "cache_read": 1.50, "cache_write": 18.75,
        },
        "claude-sonnet-4-6": {
            "input": 3.0, "output": 15.0,
            "cache_read": 0.30, "cache_write": 3.75,
        },
        "claude-haiku-4-5-20251001": {
            "input": 0.80, "output": 4.0,
            "cache_read": 0.08, "cache_write": 1.0,
        },
    }

    def __init__(self, model: str = ""):
        self.calls = []
        self.total_input = 0
        self.total_output = 0
        self.total_cache_read = 0
        self.total_cache_creation = 0
        self.model = model

    def record(self, response, turn: int):
        """Extract and record token usage from an Anthropic API response."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return

        input_tokens = getattr(usage, "input_tokens", 0)
        output_tokens = getattr(usage, "output_tokens", 0)
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0

        self.total_input += input_tokens
        self.total_output += output_tokens
        self.total_cache_read += cache_read
        self.total_cache_creation += cache_creation

        self.calls.append({
            "turn": turn,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        })

    def estimate_cost(self) -> float:
        """Estimate total cost in USD based on model pricing."""
        prices = self.PRICING.get(self.model, self.PRICING.get("claude-opus-4-6"))
        cost = (
            self.total_input * prices["input"] / 1_000_000
            + self.total_output * prices["output"] / 1_000_000
            + self.total_cache_read * prices["cache_read"] / 1_000_000
            + self.total_cache_creation * prices["cache_write"] / 1_000_000
        )
        return round(cost, 4)

    def summary(self):
        return {
            "total_input_tokens": self.total_input,
            "total_output_tokens": self.total_output,
            "total_tokens": self.total_input + self.total_output,
            "cache_read_input_tokens": self.total_cache_read,
            "cache_creation_input_tokens": self.total_cache_creation,
            "api_calls": len(self.calls),
            "estimated_cost_usd": self.estimate_cost(),
            "per_call": self.calls,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY ACCUMULATOR
# ═══════════════════════════════════════════════════════════════════════════════

class EntryAccumulator:
    """Deduplicating accumulator keyed by (kinase|substrate|site)."""

    def __init__(self):
        self._atlas = {}

    def add(self, kinase, substrate, site, uniprot="", peptide="", source=""):
        if not (kinase and substrate and site):
            return
        key = f"{kinase.strip().upper()}|{substrate.strip().upper()}|{site.strip().upper()}"
        if key not in self._atlas:
            self._atlas[key] = {
                "kinase_gene": kinase.strip(),
                "substrate_gene": substrate.strip(),
                "phospho_site": site.strip(),
                "substrate_uniprot": uniprot or "",
                "heptameric_peptide": peptide or "",
                "supporting_databases": [source] if source else [],
            }
        else:
            entry = self._atlas[key]
            if source and source not in entry["supporting_databases"]:
                entry["supporting_databases"].append(source)
            if not entry["substrate_uniprot"] and uniprot:
                entry["substrate_uniprot"] = uniprot
            if not entry["heptameric_peptide"] and peptide:
                entry["heptameric_peptide"] = peptide

    def add_bulk(self, entries, source=""):
        count = 0
        for e in entries:
            self.add(
                e.get("kinase_gene", ""),
                e.get("substrate_gene", ""),
                e.get("phospho_site", ""),
                e.get("substrate_uniprot", ""),
                e.get("heptameric_peptide", ""),
                source or e.get("source", ""),
            )
            count += 1
        return count

    def finalize(self):
        return list(self._atlas.values())

    @property
    def size(self):
        return len(self._atlas)


# ═══════════════════════════════════════════════════════════════════════════════
# WEB TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def tool_web_search(query: str, max_results: int = 10) -> dict:
    """Search via DuckDuckGo HTML (no API key needed)."""
    try:
        url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        results = []
        for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html):
            href, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
            if href.startswith("//duckduckgo.com/l/"):
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                href = parsed.get("uddg", [href])[0]
            results.append({"title": title.strip(), "url": href})
            if len(results) >= max_results:
                break
        return {"results": results, "count": len(results)}
    except Exception as e:
        return {"error": str(e), "results": []}


def tool_web_fetch(url: str, max_chars: int = 15000) -> dict:
    """Fetch a URL and return text content (truncated)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read()
            if "gzip" in content_type or url.endswith(".gz"):
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
            text = raw.decode("utf-8", errors="replace")
        return {
            "url": url,
            "content_type": content_type,
            "length": len(text),
            "content": text[:max_chars],
            "truncated": len(text) > max_chars,
        }
    except Exception as e:
        return {"error": str(e), "url": url}


def tool_fetch_and_parse(url: str, accumulator: EntryAccumulator, source_name: str = "") -> dict:
    """Fetch a URL, auto-detect format, parse phospho entries."""
    result = tool_web_fetch(url, max_chars=5_000_000)
    if "error" in result:
        return result

    content = result["content"]
    source = source_name or urllib.parse.urlparse(url).netloc

    # Try TSV/CSV parsing
    entries = []
    try:
        lines = content.strip().split("\n")
        if len(lines) > 1 and "\t" in lines[0]:
            reader = csv.DictReader(io.StringIO(content), delimiter="\t")
            for row in reader:
                kinase = row.get("GENE", row.get("KIN_ACC_ID", row.get("kinase_gene", "")))
                substrate = row.get("SUB_GENE", row.get("substrate_gene", ""))
                site = row.get("SUB_MOD_RSD", row.get("phospho_site", ""))
                uniprot = row.get("SUB_ACC_ID", row.get("substrate_uniprot", ""))
                peptide = row.get("SITE_+/-7_AA", row.get("heptameric_peptide", ""))
                if kinase and substrate and site:
                    entries.append({
                        "kinase_gene": kinase, "substrate_gene": substrate,
                        "phospho_site": site, "substrate_uniprot": uniprot,
                        "heptameric_peptide": peptide,
                    })
    except Exception:
        pass

    # Try JSON parsing
    if not entries:
        try:
            data = json.loads(content)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        entries.append(item)
        except Exception:
            pass

    added = accumulator.add_bulk(entries, source)
    return {
        "url": url, "source": source,
        "entries_parsed": len(entries), "entries_added": added,
        "atlas_size": accumulator.size,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════════════

def get_tool_definitions():
    """Return tool definitions for the Anthropic API."""
    return [
        {
            "name": "web_search",
            "description": "Search the web for information. Returns titles and URLs.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "web_fetch",
            "description": "Fetch content from a URL. Returns text (truncated to 15000 chars). Use for reading docs, API responses, download pages.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                },
                "required": ["url"],
            },
        },
        {
            "name": "fetch_and_parse_db",
            "description": "Download a database file (TSV, CSV, JSON, or gzipped), auto-parse phosphorylation entries, and accumulate them. Use for bulk downloads of kinase-substrate datasets. Returns count of entries parsed and total atlas size.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to download"},
                    "source_name": {"type": "string", "description": "Name of the database source (e.g. 'PhosphoSitePlus')"},
                },
                "required": ["url"],
            },
        },
        {
            "name": "submit_atlas",
            "description": "Submit your completed phosphorylation atlas. Call this when you have finished curating all kinase-substrate-phosphosite relationships from all databases. You can optionally include additional entries beyond what was auto-accumulated.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "additional_entries": {
                        "type": "array",
                        "description": "Optional additional entries to include",
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
                            "required": ["kinase_gene", "substrate_gene", "phospho_site"],
                        },
                    },
                    "strategy_summary": {
                        "type": "string",
                        "description": "Brief description of your curation strategy",
                    },
                },
            },
        },
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT
# ═══════════════════════════════════════════════════════════════════════════════

class ClaudeAgent:
    """Autonomous Claude agent with token tracking."""

    def __init__(self, api_key: str, model: str = "claude-opus-4-6", max_turns: int = 50):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.max_turns = max_turns
        self.accumulator = EntryAccumulator()
        self.tokens = TokenTracker(model=model)
        self.trace = []
        self.tool_count = 0
        self.strategy_summary = ""

    def _dispatch(self, tool_name, tool_input):
        """Route tool calls to implementations."""
        self.tool_count += 1

        if tool_name == "web_search":
            return tool_web_search(tool_input.get("query", ""))
        if tool_name == "web_fetch":
            return tool_web_fetch(tool_input.get("url", ""))
        if tool_name == "fetch_and_parse_db":
            return tool_fetch_and_parse(
                tool_input.get("url", ""),
                self.accumulator,
                tool_input.get("source_name", ""),
            )
        if tool_name == "submit_atlas":
            extra = tool_input.get("additional_entries", [])
            if extra:
                self.accumulator.add_bulk(extra, "agent_direct")
            self.strategy_summary = tool_input.get("strategy_summary", "")
            entries = self.accumulator.finalize()
            return {
                "status": "accepted",
                "entries_received": len(entries),
                "message": f"Atlas submitted with {len(entries)} deduplicated entries.",
            }
        return {"error": f"Unknown tool: {tool_name}"}

    def run(self, system_prompt: str):
        """Run the autonomous agent loop. Returns list of atlas entries."""
        messages = [{"role": "user", "content": "Begin."}]
        tool_defs = get_tool_definitions()
        submitted = False

        print(f"[AGENT] Model: {self.model}")
        print(f"[AGENT] Max turns: {self.max_turns}")
        print(f"[AGENT] Tools: {[t['name'] for t in tool_defs]}")

        for turn in range(self.max_turns):
            response = None
            for attempt in range(5):
                try:
                    response = self.client.messages.create(
                        model=self.model,
                        max_tokens=16384,
                        system=system_prompt,
                        messages=messages,
                        tools=tool_defs,
                    )
                    break
                except anthropic.RateLimitError:
                    wait = 15 * (attempt + 1)
                    print(f"[AGENT] Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                except (anthropic.APIConnectionError, anthropic.InternalServerError) as e:
                    wait = 10 * (attempt + 1)
                    print(f"[AGENT] Connection error, retrying in {wait}s: {e}")
                    time.sleep(wait)
                except Exception as e:
                    print(f"[AGENT] API error: {e}")
                    self.trace.append({"type": "error", "error": str(e)})
                    break

            if response is None:
                print(f"[AGENT] Failed after retries, stopping.")
                break

            # Record token usage
            self.tokens.record(response, turn)
            ts = self.tokens
            print(f"[TOKENS] Turn {turn+1}: "
                  f"in={ts.calls[-1]['input_tokens']:,} "
                  f"out={ts.calls[-1]['output_tokens']:,} "
                  f"| cumulative: {ts.total_input+ts.total_output:,} total")

            # Process response
            text_parts = []
            tool_uses = []
            for block in response.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)
                if block.type == "tool_use":
                    tool_uses.append(block)

            if text_parts:
                preview = " ".join(text_parts)[:200]
                print(f"[AGENT] Turn {turn+1}: {preview}{'...' if len(' '.join(text_parts)) > 200 else ''}")

            if not tool_uses:
                print(f"[AGENT] Finished (no tool calls). stop_reason={response.stop_reason}")
                break

            messages.append({"role": "assistant", "content": response.content})

            # Execute tool calls
            tool_results = []
            for tool_use in tool_uses:
                t0 = time.time()
                result = self._dispatch(tool_use.name, tool_use.input)
                elapsed = time.time() - t0

                result_str = json.dumps(result)
                if len(result_str) > 50000:
                    result_str = result_str[:50000] + '..."}'

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": result_str,
                })

                input_preview = json.dumps(tool_use.input)[:100]
                print(f"[TOOL {self.tool_count}] {tool_use.name}({input_preview}) "
                      f"-> {len(result_str)} chars ({elapsed:.1f}s)")

                self.trace.append({
                    "call_number": self.tool_count,
                    "tool": tool_use.name,
                    "input": tool_use.input,
                    "result_size": len(result_str),
                    "elapsed": round(elapsed, 2),
                })

                if tool_use.name == "submit_atlas":
                    submitted = True

            messages.append({"role": "user", "content": tool_results})

            if submitted:
                print(f"[AGENT] Atlas submitted. Stopping.")
                break

        entries = self.accumulator.finalize()
        if not submitted and entries:
            print(f"[AGENT] Auto-submitting {len(entries)} accumulated entries")
            self.strategy_summary = (
                f"Autonomous agent ran {self.tool_count} tool calls across {turn+1} turns. "
                f"Auto-submitted accumulated entries."
            )

        print(f"\n[AGENT] Done: {self.tool_count} tool calls, {len(entries)} entries")
        print(f"[TOKENS] Total: {self.tokens.total_input+self.tokens.total_output:,} "
              f"(input: {self.tokens.total_input:,}, output: {self.tokens.total_output:,})")
        return entries


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Run Claude agent for PhosphoAtlas curation")
    parser.add_argument("--model", default="opus", choices=list(MODEL_MAP.keys()),
                        help="Claude model variant (default: opus)")
    parser.add_argument("--condition", default="naive", choices=list(CONDITION_PROMPTS.keys()),
                        help="Prompt condition (default: naive)")
    parser.add_argument("--prompt-file", help="Custom prompt file (overrides --condition)")
    parser.add_argument("--output", required=True, help="Output directory (e.g. contributions/my_run)")
    parser.add_argument("--max-turns", type=int, default=50, help="Max agent turns (default: 50)")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: set ANTHROPIC_API_KEY environment variable")
        print("  export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    model_id = MODEL_MAP[args.model]
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    scores_dir = out_dir / "scores"
    scores_dir.mkdir(parents=True, exist_ok=True)

    # Load prompt
    if args.prompt_file:
        prompt_path = Path(args.prompt_file)
        condition = prompt_path.stem
    else:
        prompt_path = PROJECT_ROOT / CONDITION_PROMPTS[args.condition]
        condition = args.condition
    system_prompt = prompt_path.read_text().strip()

    print(f"[SETUP] Model: {model_id}")
    print(f"[SETUP] Condition: {condition}")
    print(f"[SETUP] Prompt: {prompt_path} ({len(system_prompt)} chars)")
    print(f"[SETUP] Output: {out_dir}")

    # Run agent
    t0 = time.time()
    agent = ClaudeAgent(api_key, model=model_id, max_turns=args.max_turns)
    entries = agent.run(system_prompt)
    elapsed = time.time() - t0

    print(f"\n[RESULT] Elapsed: {elapsed:.1f}s ({elapsed/60:.1f}m)")
    print(f"[RESULT] Tool calls: {agent.tool_count}")
    print(f"[RESULT] Atlas: {len(entries)} entries")

    # Save atlas
    with open(out_dir / "atlas.json", "w") as f:
        json.dump(entries, f, indent=2)

    # Compute stats
    db_counts = {}
    multi_db = 0
    for e in entries:
        for db in e.get("supporting_databases", []):
            db_counts[db] = db_counts.get(db, 0) + 1
        if len(e.get("supporting_databases", [])) >= 2:
            multi_db += 1

    # Save run_log with token usage
    run_log = {
        "agent": f"Claude {args.model.title()} ({model_id})",
        "model": model_id,
        "condition": condition,
        "prompt_file": str(prompt_path),
        "prompt_chars": len(system_prompt),
        "strategy": agent.strategy_summary,
        "databases_accessed": sorted(db_counts.keys()),
        "tool_calls": agent.tool_count,
        "raw_counts": db_counts,
        "merged_atlas": len(entries),
        "unique_kinases": len(set(e["kinase_gene"] for e in entries)),
        "unique_substrates": len(set(e["substrate_gene"] for e in entries)),
        "multi_db_entries": multi_db,
        "elapsed_seconds": round(elapsed, 1),
        "token_usage": agent.tokens.summary(),
        "trace": agent.trace,
    }
    with open(out_dir / "run_log.json", "w") as f:
        json.dump(run_log, f, indent=2)

    # Run scorer
    print("\n[SCORE] Running evaluation scorer...")
    gold_path = PROJECT_ROOT / "gold_standard" / "parsed" / "phosphoatlas_gold.json"
    if gold_path.exists():
        from evaluation.scorer import load_gold, score_atlas, score_per_kinase
        gold = load_gold(str(gold_path))
        scores = score_atlas(entries, gold)

        cl = scores["column_level"]
        summary = {k: v for k, v in scores.items()}
        summary["column_level"] = {k: v for k, v in cl.items() if k != "peptide_mismatches"}
        with open(scores_dir / "summary.json", "w") as f:
            json.dump(summary, f, indent=2)

        per_kinase = score_per_kinase(entries, gold)
        with open(scores_dir / "per_kinase.json", "w") as f:
            json.dump(per_kinase, f, indent=2)

        with open(scores_dir / "peptide_mismatches.json", "w") as f:
            json.dump(cl.get("peptide_mismatches", []), f, indent=2)

        ov = scores["overview"]
        al = scores["atlas_level"]
        print(f"[SCORE] Atlas size:       {ov['atlas_size']}")
        print(f"[SCORE] Recall:           {ov['recall']}")
        print(f"[SCORE] Precision:        {ov['precision']}")
        print(f"[SCORE] F1:               {ov['f1']}")
        print(f"[SCORE] Kinases found:    {ov['kinases_found']}")
        print(f"[SCORE] Peptide accuracy: {ov['peptide_accuracy']} (case-insensitive)")
        print(f"[SCORE] TP={al['true_positives']} FP={al['false_positives']} FN={al['false_negatives']}")

    # Print token summary
    ts = agent.tokens.summary()
    print(f"\n[COST] Token Usage Summary:")
    print(f"  Input tokens:  {ts['total_input_tokens']:,}")
    print(f"  Output tokens: {ts['total_output_tokens']:,}")
    print(f"  Total tokens:  {ts['total_tokens']:,}")
    print(f"  API calls:     {ts['api_calls']}")
    if ts['cache_read_input_tokens']:
        print(f"  Cache read:    {ts['cache_read_input_tokens']:,}")
    if ts['cache_creation_input_tokens']:
        print(f"  Cache create:  {ts['cache_creation_input_tokens']:,}")
    print(f"  Est. cost:     ${ts['estimated_cost_usd']:.2f}")

    print(f"\nOutputs in: {out_dir}/")


if __name__ == "__main__":
    main()
