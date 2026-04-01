#!/usr/bin/env python3
"""
Mistral Large agent for the PhosphoAtlas Benchmark (paper-informed).

Uses Mistral's chat completion API with generic HTTP tools so the model
can discover and query any online phosphorylation databases on its own.
The model receives the paper-informed prompt PLUS extracted text from
the PhosphoAtlas 2016 paper and its 3 supplementary files via OCR.
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
# 2) Extract paper content via Mistral OCR
# -------------------------
PAPERS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "papers"
MAIN_PAPER = PAPERS_DIR / "PhosphoAtlas_2016.pdf"
SUPPLEMENTS_DIR = PAPERS_DIR / "PhosphoAtlas_supplements"
SUPPLEMENT_FILES = sorted(SUPPLEMENTS_DIR.glob("*"))

def ocr_file(filepath: Path) -> str:
    """Upload a file to Mistral and run OCR, returning extracted markdown text."""
    print(f"  [OCR] Processing {filepath.name}...")
    suffix = filepath.suffix.lower()

    # Upload file for OCR
    with open(filepath, "rb") as f:
        uploaded = client.files.upload(
            file={"file_name": filepath.name, "content": f},
            purpose="ocr",
        )
    file_id = uploaded.id
    print(f"         Uploaded as {file_id}")

    # Run OCR
    ocr_response = client.ocr.process(
        model="mistral-ocr-latest",
        document={"type": "file", "file_id": file_id},
        table_format="markdown",
    )

    # Collect markdown from all pages
    pages_text = []
    for page in ocr_response.pages:
        pages_text.append(page.markdown)

    text = "\n\n---\n\n".join(pages_text)
    print(f"         Extracted {len(text)} chars from {len(ocr_response.pages)} pages")
    return text


# Maximum chars to keep from OCR output per document (to avoid blowing up context)
MAX_PAPER_CHARS = 40000
MAX_SUPPLEMENT_CHARS = 15000

def truncate_text(text: str, max_chars: int, label: str) -> str:
    """Truncate text to max_chars, adding a note if truncated."""
    if len(text) <= max_chars:
        return text
    print(f"  [TRUNCATE] {label}: {len(text)} -> {max_chars} chars")
    return text[:max_chars] + f"\n\n... [TRUNCATED — original was {len(text)} chars]"


print("[OCR] Extracting text from PhosphoAtlas paper and supplements...")

paper_text = truncate_text(ocr_file(MAIN_PAPER), MAX_PAPER_CHARS, "Main paper")

supplement_texts = []
for sup_file in SUPPLEMENT_FILES:
    try:
        sup_text = ocr_file(sup_file)
        sup_text = truncate_text(sup_text, MAX_SUPPLEMENT_CHARS, sup_file.name)
        supplement_texts.append((sup_file.name, sup_text))
    except Exception as e:
        print(f"  [OCR] WARNING: Failed to OCR {sup_file.name}: {e}")

print(f"[OCR] Done. Main paper: {len(paper_text)} chars, "
      f"Supplements: {len(supplement_texts)} files processed\n")

# -------------------------
# 3) Generic HTTP tool implementations
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
# 4) Prompt (paper-informed — from agents/prompts/paper_informed.txt, no modifications)
# -------------------------
PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "agents" / "prompts" / "paper_informed.txt"
BASE_PROMPT = PROMPT_PATH.read_text()

# Build the full prompt with paper context
supplement_section = ""
for name, text in supplement_texts:
    supplement_section += f"\n\n### Supplement: {name}\n{text}"

PROMPT = (
    f"{BASE_PROMPT}\n\n"
    f"--- REFERENCE MATERIAL ---\n\n"
    f"Below is the full text of the PhosphoAtlas paper (Olow et al., 2016) "
    f"and its supplementary files, extracted via OCR. Use this information "
    f"to guide your database querying strategy.\n\n"
    f"## PhosphoAtlas 2016 Paper\n{paper_text}\n\n"
    f"## Supplementary Files{supplement_section}\n\n"
    f"--- END REFERENCE MATERIAL ---\n\n"
    f"Now proceed with your systematic curation. Start by querying the databases "
    f"mentioned in the paper above, then expand to any additional sources you can find."
)

# -------------------------
# 5) Run the agent loop
# -------------------------
messages = [
    {"role": "user", "content": PROMPT},
]

MAX_TURNS = 200
MAX_RETRIES = 5
turn = 0
atlas = None
strategy_summary = ""
t0 = time.time()


def chat_complete_with_retry(client, **kwargs):
    """Call chat.complete with exponential backoff on transient errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return client.chat.complete(**kwargs)
        except Exception as e:
            err_str = str(e)
            is_transient = any(code in err_str for code in ["503", "502", "429", "500", "unreachable"])
            if is_transient and attempt < MAX_RETRIES:
                wait = 2 ** attempt  # 2, 4, 8, 16, 32 seconds
                print(f"  [RETRY] Attempt {attempt}/{MAX_RETRIES} failed ({err_str[:80]}), "
                      f"retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise


print("[START] Running Mistral Large paper-informed agent with HTTP tools")

while turn < MAX_TURNS:
    turn += 1
    print(f"[TURN {turn}] Calling model...")

    response = chat_complete_with_retry(
        client,
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
# 6) Save outputs
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
    "condition": "paper_informed",
    "strategy_summary": strategy_summary,
    "databases_accessed": sorted(db_counts.keys()),
    "tool_calls": turn,
    "turns": turn,
    "elapsed_seconds": round(elapsed, 1),
    "atlas_size": len(atlas),
    "unique_kinases": len(set(e.get("kinase_gene", "") for e in atlas)),
    "unique_substrates": len(set(e.get("substrate_gene", "") for e in atlas)),
    "multi_db_entries": multi_db_count,
    "paper_context": {
        "main_paper": str(MAIN_PAPER),
        "supplements": [str(f) for f in SUPPLEMENT_FILES],
        "main_paper_chars": len(paper_text),
        "supplement_chars": sum(len(t) for _, t in supplement_texts),
    },
}
with open(out_dir / "run_log.json", "w") as f:
    json.dump(run_log, f, indent=2)

# -------------------------
# 7) Run scorer if gold standard exists
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
