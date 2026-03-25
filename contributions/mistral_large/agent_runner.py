#!/usr/bin/env python3
"""
Mistral Large agent for the PhosphoAtlas Benchmark (naive zero-shot).

Uses Mistral's chat completion API with generic HTTP tools so the model
can discover and query any online phosphorylation databases on its own.
The model receives ONLY the naive prompt — no hints about which
databases exist or how to query them.
"""
import json
import os
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from mistralai.client import Mistral

load_dotenv()

# -------------------------
# 1) Connect to Mistral
# -------------------------
api_key = os.getenv("MISTRAL_API_KEY")
if not api_key:
    raise ValueError("MISTRAL_API_KEY not set in environment")

client = Mistral(api_key=api_key)

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
# 3) Prompt (naive zero-shot — from agents/prompts/naive.txt, no modifications)
# -------------------------
PROMPT = (
    "You are a bioinformatics researcher tasked with building a comprehensive "
    "human protein phosphorylation atlas from available databases.\n\n"
    "Your goal: Curate ALL known human kinase-substrate-phosphosite relationships "
    "by systematically querying the databases available to you.\n\n"
    "For each relationship, you must capture:\n"
    "  - Kinase gene symbol (the enzyme)\n"
    "  - Substrate gene symbol (the target protein)\n"
    "  - Phosphorylation site (e.g., Y15, S10, T161)\n"
    "  - Heptameric peptide sequence around the site (if available)\n"
    "  - Substrate UniProt accession (if available)\n"
    "  - Which database(s) support this relationship\n\n"
    "Requirements:\n"
    "  1. Be EXHAUSTIVE — the atlas should contain every kinase-substrate-site "
    "triplet present in the databases. Missing entries is worse than having extra entries.\n"
    "  2. Cross-reference across databases — if the same relationship appears in "
    "multiple databases, record all supporting sources.\n"
    "  3. Do NOT fabricate data. Only include relationships returned by the tools.\n\n"
    "Start by discovering what databases are available, then develop and execute "
    "a systematic curation strategy.\n\n"
    "When you are finished, call the \"submit_atlas\" tool with your complete results."
)

# -------------------------
# 4) Run the agent loop
# -------------------------
messages = [
    {"role": "user", "content": PROMPT},
]

MAX_TURNS = 200
turn = 0
atlas = None
strategy_summary = ""
t0 = time.time()

print("[START] Running Mistral Large naive zero-shot agent with HTTP tools")

while turn < MAX_TURNS:
    turn += 1
    print(f"[TURN {turn}] Calling model...")

    response = client.chat.complete(
        model="mistral-large-latest",
        messages=messages,
        tools=TOOL_DEFINITIONS,
        tool_choice="auto",
        temperature=0.3,
        top_p=0.95,
    )

    choice = response.choices[0]
    message = choice.message

    if message.tool_calls:
        # Add assistant message with tool calls
        messages.append({
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
        })

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

    # No tool calls — text response
    text = message.content or ""
    print(f"  [TEXT] {text[:200]}...")
    messages.append({"role": "assistant", "content": text})

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

run_log = {
    "agent": "Mistral Large (mistral-large-latest)",
    "condition": "naive",
    "strategy_summary": strategy_summary,
    "databases_accessed": sorted(db_counts.keys()),
    "tool_calls": turn,
    "turns": turn,
    "elapsed_seconds": round(elapsed, 1),
    "atlas_size": len(atlas),
    "unique_kinases": len(set(e.get("kinase_gene", "") for e in atlas)),
    "unique_substrates": len(set(e.get("substrate_gene", "") for e in atlas)),
    "multi_db_entries": multi_db_count,
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
