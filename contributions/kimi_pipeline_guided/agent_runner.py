#!/usr/bin/env python3
"""
Kimi (Moonshot AI) agent for the PhosphoAtlas Benchmark (pipeline-guided).

Uses an OpenAI-compatible Kimi endpoint with generic HTTP tools. The
model receives the docx-inline pipeline-guided prompt
(agents/prompts/naive_plus_suppl.txt), which embeds the Olow et al.
2016 supplementary methods (STEP 1 – STEP 6) directly in text — no
runtime OCR required.

Set KIMI_API_KEY, KIMI_BASE_URL, KIMI_MODEL in .env. See CLAUDE.md
for outstanding provider/model/pricing TODOs.
"""
import json
import os
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# -------------------------
# 1) Connect to Kimi (OpenAI-compatible endpoint)
# -------------------------
api_key = os.getenv("KIMI_API_KEY")
base_url = os.getenv("KIMI_BASE_URL")
if not api_key:
    raise ValueError("KIMI_API_KEY not set in environment")
if not base_url:
    raise ValueError("KIMI_BASE_URL not set in environment (e.g. https://api.moonshot.ai/v1)")

client = OpenAI(api_key=api_key, base_url=base_url, timeout=1800.0)  # 30 min — kimi-k2.6 with max_tokens=131072 can take a while when reasoning is heavy

# Moonshot model ID. "kimi-k2.6" is the current K2 generation (256K context).
# Override via KIMI_MODEL env var if testing a different variant (e.g. kimi-k2.5).
MODEL = os.getenv("KIMI_MODEL", "kimi-k2.6")

# Moonshot pricing for kimi-k2.6 (USD per 1M tokens, as of 2026-05-27).
# Cached and fresh input tokens are billed at different rates. We read
# usage.prompt_tokens_details.cached_tokens and price each bucket at its
# real rate so the run_log.json cost matches Moonshot's actual bill.
KIMI_COST_PER_1M_INPUT_CACHE_HIT = 0.16   # USD per 1M cached input tokens
KIMI_COST_PER_1M_INPUT_CACHE_MISS = 0.95  # USD per 1M uncached input tokens
KIMI_COST_PER_1M_OUTPUT = 4.00            # USD per 1M output tokens

# -------------------------
# 2) Generic HTTP tool implementations
# -------------------------

def http_get(url, headers=None):
    """Fetch a URL and return the response body as text."""
    h = {"User-Agent": "PhosphoAtlas-Agent/1.0"}
    if headers:
        try:
            h.update(json.loads(headers))
        except (json.JSONDecodeError, TypeError):
            pass
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if len(body) > 15000:
                body = body[:15000] + f"\n\n... [TRUNCATED — response was {len(body)} chars. If you need more data, try a more specific query or use pagination parameters.]"
            return {"status": "ok", "body": body}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def http_post(url, body=None, headers=None):
    """POST to a URL and return the response body as text."""
    h = {"User-Agent": "PhosphoAtlas-Agent/1.0", "Content-Type": "application/json"}
    if headers:
        try:
            h.update(json.loads(headers))
        except (json.JSONDecodeError, TypeError):
            pass
    data = body.encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            response_body = resp.read().decode("utf-8", errors="replace")
            if len(response_body) > 15000:
                response_body = response_body[:15000] + f"\n\n... [TRUNCATED — response was {len(response_body)} chars. If you need more data, try a more specific query or use pagination parameters.]"
            return {"status": "ok", "body": response_body}
    except Exception as e:
        return {"status": "error", "error": str(e)}


TOOL_FUNCTIONS = {
    "http_get": http_get,
    "http_post": http_post,
}

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "http_get",
            "description": "Make an HTTP GET request to any URL. Use this to query online databases, APIs, and data sources.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch"},
                    "headers": {"type": "string", "description": "Optional JSON string of extra headers"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_post",
            "description": "Make an HTTP POST request to any URL. Use this for APIs that require POST requests.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to POST to"},
                    "body": {"type": "string", "description": "Request body (typically JSON string)"},
                    "headers": {"type": "string", "description": "Optional JSON string of extra headers"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_atlas",
            "description": (
                "Submit your completed phosphorylation atlas. "
                "Call this when you have finished curating all "
                "kinase-substrate-phosphosite relationships."
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
                            "required": ["kinase_gene", "substrate_gene", "phospho_site"],
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
    },
]

# -------------------------
# 3) Prompt (pipeline-guided — from agents/prompts/naive_plus_suppl.txt, no modifications)
# -------------------------
PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "agents" / "prompts" / "naive_plus_suppl.txt"
PROMPT = PROMPT_PATH.read_text()

# -------------------------
# 4) Run the agent loop
# -------------------------
messages = [
    {"role": "user", "content": PROMPT},
]

MAX_TURNS = 200  # safety net only; real termination is driven by context pressure
MAX_RETRIES = 5
KIMI_CONTEXT_WINDOW_TOKENS = 262_144  # kimi-k2.6 max input
CONTEXT_PRESSURE_THRESHOLD = 200_000  # nudge submit_atlas once prompt_tokens crosses this
turn = 0
atlas = None
strategy_summary = ""
total_input_tokens_cached = 0
total_input_tokens_uncached = 0
total_output_tokens = 0
last_prompt_tokens = 0  # most recent request's prompt_tokens — drives context-pressure nudging
per_turn_usage = []
t0 = time.time()


def chat_complete_with_retry(client, **kwargs):
    """Call chat.completions.create with exponential backoff on transient errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:
            err_str = str(e)
            is_transient = any(code in err_str for code in ["503", "502", "429", "500", "unreachable", "timed out", "timeout", "ReadTimeout", "APITimeout"])
            if is_transient and attempt < MAX_RETRIES:
                wait = 2 ** attempt  # 2, 4, 8, 16, 32 seconds
                print(f"  [RETRY] Attempt {attempt}/{MAX_RETRIES} failed ({err_str[:80]}), "
                      f"retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise


print(f"[START] Running Kimi pipeline-guided agent with HTTP tools (model={MODEL})")

# Force the model to submit before context fills up. Moonshot's kimi-k2.6
# rejects tool_choice=specified on thinking models, so we can't force
# submit_atlas via the API. Instead we watch prompt_tokens from each
# response and start re-injecting a hard "submit now" imperative once we
# cross CONTEXT_PRESSURE_THRESHOLD. Lets the model explore freely until
# we're genuinely close to Moonshot's 262K window — no arbitrary turn cap.

while turn < MAX_TURNS:
    turn += 1
    print(f"[TURN {turn}] Calling model...")

    if last_prompt_tokens >= CONTEXT_PRESSURE_THRESHOLD and atlas is None:
        pct = last_prompt_tokens * 100 // KIMI_CONTEXT_WINDOW_TOKENS
        print(f"  [NUDGE] context at {pct}% of window — demanding submit_atlas")
        messages.append({
            "role": "user",
            "content": (
                f"Your context window is nearly full ({last_prompt_tokens:,} "
                f"of {KIMI_CONTEXT_WINDOW_TOKENS:,} tokens used). Your NEXT "
                "response MUST be a submit_atlas tool call with the data you "
                "have collected so far. Do NOT make any more http_get or "
                "http_post calls. If you have no data, submit an empty atlas. "
                "Submit immediately."
            ),
        })

    response = chat_complete_with_retry(
        client,
        model=MODEL,
        messages=messages,
        tools=TOOL_DEFINITIONS,
        tool_choice="auto",
        temperature=1,  # kimi-k2.6 only accepts temperature=1
        max_tokens=131072,  # raise from Moonshot's 32K default — reasoning_content was eating the whole output budget, truncating submit_atlas args and producing empty atlases
    )

    # Track token usage. Split prompt_tokens into cached vs uncached so the
    # cost reflects Moonshot's per-bucket pricing.
    usage = getattr(response, "usage", None)
    if usage:
        inp = getattr(usage, "prompt_tokens", 0) or 0
        out = getattr(usage, "completion_tokens", 0) or 0
        cached = 0
        try:
            cached = (response.usage.prompt_tokens_details.cached_tokens or 0)
        except (AttributeError, TypeError):
            pass
        uncached = max(inp - cached, 0)
        total_input_tokens_cached += cached
        total_input_tokens_uncached += uncached
        total_output_tokens += out
        per_turn_usage.append({
            "turn": turn,
            "input_tokens_cached": cached,
            "input_tokens_uncached": uncached,
            "output_tokens": out,
        })
        cost_so_far = (total_input_tokens_cached * KIMI_COST_PER_1M_INPUT_CACHE_HIT +
                       total_input_tokens_uncached * KIMI_COST_PER_1M_INPUT_CACHE_MISS +
                       total_output_tokens * KIMI_COST_PER_1M_OUTPUT) / 1_000_000
        last_prompt_tokens = inp
        print(f"  [TOKENS] in={inp:,} (cached={cached:,}) out={out:,} | cumulative ${cost_so_far:.4f}")

    choice = response.choices[0]
    message = choice.message

    if message.tool_calls:
        # Add assistant message with tool calls. kimi-k2.6 is a thinking
        # model — the API rejects subsequent turns if reasoning_content is
        # dropped from the echoed assistant message, so preserve it when
        # present.
        assistant_msg = {
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
            ],
        }
        reasoning_content = getattr(message, "reasoning_content", None)
        if reasoning_content:
            assistant_msg["reasoning_content"] = reasoning_content
        messages.append(assistant_msg)

        # Execute each tool call and add results
        for tc in message.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}

            if name == "submit_atlas":
                atlas = args.get("entries", [])
                strategy_summary = args.get("strategy_summary", "")
                print(f"  [SUBMIT] Atlas received: {len(atlas)} entries")
                result = {"status": "accepted", "entries_received": len(atlas)}
            elif name in TOOL_FUNCTIONS:
                url_display = args.get("url", "")[:80]
                print(f"  [{name.upper()}] {url_display}")
                try:
                    result = TOOL_FUNCTIONS[name](**args)
                    if result.get("status") == "ok":
                        body_len = len(result.get("body", ""))
                        print(f"         -> ok ({body_len} chars)")
                    else:
                        print(f"         -> ERROR: {result.get('error', 'unknown')}")
                except Exception as e:
                    print(f"         -> ERROR: {e}")
                    result = {"status": "error", "error": str(e)}
            else:
                result = {"status": "error", "error": f"Unknown tool: {name}"}

            messages.append({
                "role": "tool",
                "name": name,
                "content": json.dumps(result, default=str),
                "tool_call_id": tc.id,
            })

            if atlas is not None:
                break

        if atlas is not None:
            break
        continue

    # No tool calls — text response. Moonshot rejects empty assistant
    # messages on subsequent turns, and kimi-k2.6 can return a turn whose
    # visible content is empty (the model "thought" but didn't speak). We
    # echo reasoning_content and use a placeholder content so the message
    # is non-empty for the next request.
    text = message.content or ""
    reasoning_content = getattr(message, "reasoning_content", None)
    print(f"  [TEXT] {text[:200]}...")
    msg = {"role": "assistant", "content": text if text else "[thinking only — no surface text]"}
    if reasoning_content:
        msg["reasoning_content"] = reasoning_content
    messages.append(msg)

    if atlas is not None:
        break

    # Nudge the model to keep going
    messages.append({
        "role": "user",
        "content": "Continue. You have not called submit_atlas yet.",
    })

elapsed = time.time() - t0
print(f"[DONE] Turns: {turn}, Atlas entries: {len(atlas) if atlas else 0}, "
      f"Elapsed: {elapsed:.1f}s")

# -------------------------
# 5) Save outputs
# -------------------------
if atlas is None:
    atlas = []

out_dir = Path(__file__).parent
scores_dir = out_dir / "scores"
scores_dir.mkdir(parents=True, exist_ok=True)

# Save atlas.json
atlas_path = out_dir / "atlas.json"
with open(atlas_path, "w") as f:
    json.dump(atlas, f, indent=2, default=str)

# Save run_log.json
db_counts = {}
multi_db_count = 0
for e in atlas:
    for db in e.get("supporting_databases", []):
        db_counts[db] = db_counts.get(db, 0) + 1
    if len(e.get("supporting_databases", [])) >= 2:
        multi_db_count += 1

token_cost = (total_input_tokens_cached * KIMI_COST_PER_1M_INPUT_CACHE_HIT +
              total_input_tokens_uncached * KIMI_COST_PER_1M_INPUT_CACHE_MISS +
              total_output_tokens * KIMI_COST_PER_1M_OUTPUT) / 1_000_000

run_log = {
    "agent": f"Kimi ({MODEL})",
    "model": MODEL,
    "provider_base_url": base_url,
    "condition": "pipeline_guided",
    "strategy_summary": strategy_summary,
    "databases_accessed": sorted(db_counts.keys()),
    "tool_calls": turn,
    "turns": turn,
    "elapsed_seconds": round(elapsed, 1),
    "atlas_size": len(atlas),
    "unique_kinases": len(set(e.get("kinase_gene", "") for e in atlas)),
    "unique_substrates": len(set(e.get("substrate_gene", "") for e in atlas)),
    "multi_db_entries": multi_db_count,
    "token_usage": {
        "total_input_tokens": total_input_tokens_cached + total_input_tokens_uncached,
        "total_input_tokens_cached": total_input_tokens_cached,
        "total_input_tokens_uncached": total_input_tokens_uncached,
        "total_output_tokens": total_output_tokens,
        "total_tokens": total_input_tokens_cached + total_input_tokens_uncached + total_output_tokens,
        "estimated_cost_usd": round(token_cost, 4),
        "api_calls": len(per_turn_usage),
        "pricing": {
            "provider": "Moonshot AI (Kimi)",
            "input_per_1m_cache_hit": KIMI_COST_PER_1M_INPUT_CACHE_HIT,
            "input_per_1m_cache_miss": KIMI_COST_PER_1M_INPUT_CACHE_MISS,
            "output_per_1m": KIMI_COST_PER_1M_OUTPUT,
        },
        "per_call": per_turn_usage,
    },
}
with open(out_dir / "run_log.json", "w") as f:
    json.dump(run_log, f, indent=2)

# -------------------------
# 6) Run scorer if gold standard exists
# -------------------------
import sys
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

gold_path = PROJECT_ROOT / "gold_standard" / "parsed" / "phosphoatlas_gold.json"
if gold_path.exists() and len(atlas) > 0:
    print("\n[SCORE] Running evaluation scorer...")
    from evaluation.scorer import load_gold, score_atlas, score_per_kinase

    gold = load_gold(str(gold_path))
    scores = score_atlas(atlas, gold)

    cl = scores["column_level"]
    summary = {k: v for k, v in scores.items()}
    summary["column_level"] = {k: v for k, v in cl.items() if k != "peptide_mismatches"}
    with open(scores_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    per_kinase = score_per_kinase(atlas, gold)
    with open(scores_dir / "per_kinase.json", "w") as f:
        json.dump(per_kinase, f, indent=2)

    with open(scores_dir / "peptide_mismatches.json", "w") as f:
        json.dump(cl.get("peptide_mismatches", []), f, indent=2)

    ov = scores["overview"]
    print(f"  Atlas size:       {ov['atlas_size']}")
    print(f"  Recall:           {ov['recall']}")
    print(f"  Precision:        {ov['precision']}")
    print(f"  F1:               {ov['f1']}")
    print(f"  Kinases found:    {ov['kinases_found']}")
    print(f"  Peptide accuracy: {ov['peptide_accuracy']}")
else:
    if len(atlas) == 0:
        print("\n[SCORE] Skipping scoring — atlas is empty")
    else:
        print(f"\n[SCORE] Gold standard not found at {gold_path}, skipping scoring")

print(f"\nOutputs in: {out_dir}/")
print(f"  atlas.json, run_log.json")
if len(atlas) > 0:
    print(f"  scores/summary.json, scores/per_kinase.json, scores/peptide_mismatches.json")
