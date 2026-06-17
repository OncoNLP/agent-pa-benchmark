#!/usr/bin/env python3
"""
Kimi (Moonshot AI) discovery runner for the Dark_Kinases task.

Tier-0 baseline: single Kimi conversation with the dark_kinases prompt
as-drafted. The agent DISCOVERS the IDG / DKK dark kinome (e.g. via
Pharos T-dark / T-bio filtering or low PubMed-count heuristics), then
curates each, then submits all per-kinase records in one
`submit_dark_kinases_atlas` call. Empty `substrates` arrays are
expected for the majority of records.
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

api_key = os.getenv("KIMI_API_KEY")
base_url = os.getenv("KIMI_BASE_URL")
if not api_key:
    raise ValueError("KIMI_API_KEY not set in environment")
if not base_url:
    raise ValueError("KIMI_BASE_URL not set in environment (e.g. https://api.moonshot.ai/v1)")

client = OpenAI(api_key=api_key, base_url=base_url, timeout=1800.0)
MODEL = os.getenv("KIMI_MODEL", "kimi-k2.6")

KIMI_COST_PER_1M_INPUT_CACHE_HIT = 0.16
KIMI_COST_PER_1M_INPUT_CACHE_MISS = 0.95
KIMI_COST_PER_1M_OUTPUT = 4.00


def http_get(url, headers=None):
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
                body = body[:15000] + f"\n\n... [TRUNCATED — response was {len(body)} chars.]"
            return {"status": "ok", "body": body}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def http_post(url, body=None, headers=None):
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
                response_body = response_body[:15000] + f"\n\n... [TRUNCATED — response was {len(response_body)} chars.]"
            return {"status": "ok", "body": response_body}
    except Exception as e:
        return {"status": "error", "error": str(e)}


TOOL_FUNCTIONS = {"http_get": http_get, "http_post": http_post}

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "http_get",
            "description": "HTTP GET. UniProt, Pharos/IDG, PubMed, PhosphoSitePlus, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "headers": {"type": "string"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_post",
            "description": "HTTP POST.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "body": {"type": "string"},
                    "headers": {"type": "string"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_dark_kinases_atlas",
            "description": (
                "Submit ALL curated dark-kinase records as a single batch. The `records` "
                "array must contain one entry per kinase you classified as dark. Empty "
                "`substrates` arrays are expected for the majority of records — submit them "
                "anyway so the eval knows the kinase was considered. Call exactly once."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "records": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kinase_gene": {"type": "string"},
                                "kinase_uniprot": {"type": "string"},
                                "kinase_family": {"type": "string", "description": "Manning kinome group or empty."},
                                "idg_status": {"type": "string", "description": "Pharos/IDG TDL: Tdark/Tbio/Tchem/Tclin/unknown."},
                                "pubmed_count": {"type": "integer"},
                                "kinase_citations": {"type": "array", "items": {"type": "string"}},
                                "substrates": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "kinase_gene": {"type": "string"},
                                            "substrate_gene": {"type": "string"},
                                            "substrate_uniprot": {"type": "string"},
                                            "phospho_site": {"type": "string"},
                                            "heptameric_peptide": {"type": "string"},
                                            "supporting_databases": {"type": "array", "items": {"type": "string"}},
                                            "evidence_level": {"type": "string"},
                                            "citations": {"type": "array", "items": {"type": "string"}},
                                        },
                                        "required": ["kinase_gene", "substrate_gene", "phospho_site", "evidence_level", "citations"],
                                    },
                                },
                            },
                            "required": ["kinase_gene", "idg_status", "substrates"],
                        },
                    },
                    "strategy_summary": {"type": "string"},
                },
                "required": ["records"],
            },
        },
    },
]


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
PROMPT_PATH = PROJECT_ROOT / "agents" / "prompts" / "dark_kinases.txt"
PROMPT = PROMPT_PATH.read_text()


messages = [{"role": "user", "content": PROMPT}]

MAX_TURNS = 200
MAX_RETRIES = 5
KIMI_CONTEXT_WINDOW_TOKENS = 262_144
CONTEXT_PRESSURE_THRESHOLD = 200_000

turn = 0
records = None
strategy_summary = ""
total_input_tokens_cached = 0
total_input_tokens_uncached = 0
total_output_tokens = 0
last_prompt_tokens = 0
per_turn_usage = []
t0 = time.time()


def chat_complete_with_retry(client, **kwargs):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:
            err_str = str(e)
            is_transient = any(code in err_str for code in ["503", "502", "429", "500", "unreachable", "timed out", "timeout", "ReadTimeout", "APITimeout"])
            if is_transient and attempt < MAX_RETRIES:
                wait = 2 ** attempt
                print(f"  [RETRY] attempt {attempt}/{MAX_RETRIES} failed ({err_str[:80]}), sleeping {wait}s")
                time.sleep(wait)
            else:
                raise


print(f"[START] Kimi Dark_Kinases discovery runner (model={MODEL})")

while turn < MAX_TURNS:
    turn += 1
    print(f"[TURN {turn}] Calling model...")

    if last_prompt_tokens >= CONTEXT_PRESSURE_THRESHOLD and records is None:
        pct = last_prompt_tokens * 100 // KIMI_CONTEXT_WINDOW_TOKENS
        print(f"  [NUDGE] context at {pct}% — demanding submit_dark_kinases_atlas")
        messages.append({
            "role": "user",
            "content": (
                f"Your context window is nearly full ({last_prompt_tokens:,} of "
                f"{KIMI_CONTEXT_WINDOW_TOKENS:,} tokens). Your NEXT response MUST be a "
                "submit_dark_kinases_atlas tool call with the records you have. Submit immediately."
            ),
        })

    response = chat_complete_with_retry(
        client,
        model=MODEL,
        messages=messages,
        tools=TOOL_DEFINITIONS,
        tool_choice="auto",
        temperature=1,
        max_tokens=131072,
    )

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
        assistant_msg = {
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in message.tool_calls
            ],
        }
        reasoning_content = getattr(message, "reasoning_content", None)
        if reasoning_content:
            assistant_msg["reasoning_content"] = reasoning_content
        messages.append(assistant_msg)

        for tc in message.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}

            if name == "submit_dark_kinases_atlas":
                records = args.get("records", [])
                strategy_summary = args.get("strategy_summary", "")
                print(f"  [SUBMIT] received {len(records)} dark-kinase records")
                result = {"status": "accepted", "records_received": len(records)}
            elif name in TOOL_FUNCTIONS:
                url_display = args.get("url", "")[:80]
                print(f"  [{name.upper()}] {url_display}")
                try:
                    result = TOOL_FUNCTIONS[name](**args)
                    if result.get("status") == "ok":
                        print(f"         -> ok ({len(result.get('body', ''))} chars)")
                    else:
                        print(f"         -> ERROR: {result.get('error', 'unknown')[:120]}")
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

            if records is not None:
                break

        if records is not None:
            break
        continue

    text = message.content or ""
    reasoning_content = getattr(message, "reasoning_content", None)
    print(f"  [TEXT] {text[:200]}")
    msg = {"role": "assistant", "content": text if text else "[thinking only — no surface text]"}
    if reasoning_content:
        msg["reasoning_content"] = reasoning_content
    messages.append(msg)

    if records is not None:
        break

    messages.append({
        "role": "user",
        "content": "Continue. You have not called submit_dark_kinases_atlas yet.",
    })

elapsed = time.time() - t0
if records is None:
    records = []
print(f"[DONE] Turns: {turn}, Records: {len(records)}, Elapsed: {elapsed:.1f}s")


def flatten_records(recs):
    flat = []
    for r in recs:
        kg = r.get("kinase_gene", "")
        for sub in r.get("substrates", []):
            flat.append({
                "kinase_gene": sub.get("kinase_gene") or kg,
                "substrate_gene": sub.get("substrate_gene", ""),
                "substrate_uniprot": sub.get("substrate_uniprot", ""),
                "phospho_site": sub.get("phospho_site", ""),
                "heptameric_peptide": sub.get("heptameric_peptide", ""),
                "supporting_databases": sub.get("supporting_databases", []),
                "evidence_level": sub.get("evidence_level", ""),
                "citations": sub.get("citations", []),
            })
    return flat


atlas = flatten_records(records)

out_dir = Path(__file__).resolve().parent
scores_dir = out_dir / "scores"
scores_dir.mkdir(parents=True, exist_ok=True)

with open(out_dir / "records.json", "w") as f:
    json.dump(records, f, indent=2, default=str)
with open(out_dir / "atlas.json", "w") as f:
    json.dump(atlas, f, indent=2, default=str)


db_counts = {}
multi_db = 0
for e in atlas:
    dbs = e.get("supporting_databases", [])
    for db in dbs:
        db_counts[db] = db_counts.get(db, 0) + 1
    if len(dbs) >= 2:
        multi_db += 1

token_cost = (total_input_tokens_cached * KIMI_COST_PER_1M_INPUT_CACHE_HIT +
              total_input_tokens_uncached * KIMI_COST_PER_1M_INPUT_CACHE_MISS +
              total_output_tokens * KIMI_COST_PER_1M_OUTPUT) / 1_000_000

# IDG-status histogram for run-log color.
idg_hist = {}
for r in records:
    s = r.get("idg_status", "unknown")
    idg_hist[s] = idg_hist.get(s, 0) + 1

run_log = {
    "agent": f"Kimi ({MODEL})",
    "model": MODEL,
    "provider_base_url": base_url,
    "condition": "dark_kinases",
    "tier": "iteration_0_baseline",
    "strategy_summary": strategy_summary,
    "turns": turn,
    "elapsed_seconds": round(elapsed, 1),
    "records_submitted": len(records),
    "atlas_size_flat": len(atlas),
    "unique_kinases": len(set(e.get("kinase_gene", "") for e in atlas)),
    "unique_substrates": len(set(e.get("substrate_gene", "") for e in atlas)),
    "idg_status_histogram": idg_hist,
    "multi_db_entries": multi_db,
    "databases_accessed": sorted(db_counts.keys()),
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


sys.path.insert(0, str(PROJECT_ROOT))
gold_path = PROJECT_ROOT / "gold_standard" / "parsed" / "phosphoatlas_gold.json"
if gold_path.exists() and len(atlas) > 0:
    print("\n[SCORE] Running evaluation scorer...")
    print("        (Recall against gold is expected to be small — dark kinases by definition")
    print("         have little curated substrate data in PSP/SIGNOR, which the gold derives from.)")
    from evaluation.scorer import load_gold, score_atlas, score_per_kinase

    gold = load_gold(str(gold_path))
    scores = score_atlas(atlas, gold)
    cl = scores["column_level"]
    summary = {k: v for k, v in scores.items()}
    summary["column_level"] = {k: v for k, v in cl.items() if k != "peptide_mismatches"}

    with open(scores_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    with open(scores_dir / "per_kinase.json", "w") as f:
        json.dump(score_per_kinase(atlas, gold), f, indent=2)
    with open(scores_dir / "peptide_mismatches.json", "w") as f:
        json.dump(cl.get("peptide_mismatches", []), f, indent=2)

    ov = scores["overview"]
    print(f"  Atlas size:       {ov['atlas_size']}")
    print(f"  Recall:           {ov['recall']}")
    print(f"  Precision:        {ov['precision']}")
    print(f"  Kinases found:    {ov['kinases_found']}")
elif len(atlas) == 0:
    print("\n[SCORE] Skipping scoring — atlas is empty (expected for dark kinases).")
else:
    print(f"\n[SCORE] Gold standard not found at {gold_path}, skipping scoring")

print(f"\nOutputs in: {out_dir}/")
