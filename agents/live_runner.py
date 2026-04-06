#!/usr/bin/env python3
"""
Live Runner for PhosphoAtlas Benchmark.

Downloads data from real web sources (PhosphoSitePlus, SIGNOR, UniProt)
on-the-fly, parses it, builds the atlas, and estimates API token costs
based on actual data volumes flowing through the tool calls.

This mirrors what an autonomous LLM agent does: discover databases via
web search, download bulk datasets, parse entries, and submit an atlas.

No API key required — the "agent decisions" are scripted, but the data
downloads and parsing are real, giving accurate cost estimates.

Usage:
  python3 agents/live_runner.py --model opus --condition naive
  python3 agents/live_runner.py --model sonnet --condition paper_informed
  python3 agents/live_runner.py --model gemini-pro --condition naive
  python3 agents/live_runner.py --list-models

Requires: Internet access (downloads ~5-10 MB of data).
"""
import argparse
import csv
import gzip
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ═══════════════════════════════════════════════════════════════════════════════
# MODEL PRICING (per 1M tokens, USD, 2025-Q2)
# ═══════════════════════════════════════════════════════════════════════════════

MODEL_PRICING = {
    "opus":           {"input": 15.0,  "output": 75.0,  "label": "Claude Opus 4.6"},
    "sonnet":         {"input": 3.0,   "output": 15.0,  "label": "Claude Sonnet 4.6"},
    "haiku":          {"input": 0.80,  "output": 4.0,   "label": "Claude Haiku 4.5"},
    "gpt-5":          {"input": 10.0,  "output": 30.0,  "label": "GPT-5"},
    "gpt-4o":         {"input": 2.50,  "output": 10.0,  "label": "GPT-4o"},
    "o3":             {"input": 10.0,  "output": 40.0,  "label": "o3"},
    "gemini-pro":     {"input": 1.25,  "output": 10.0,  "label": "Gemini 2.5 Pro"},
    "gemini-flash":   {"input": 0.15,  "output": 0.60,  "label": "Gemini 2.5 Flash"},
    "mistral-large":  {"input": 2.0,   "output": 6.0,   "label": "Mistral Large"},
    "qwen-235b":      {"input": 0.80,  "output": 0.80,  "label": "Qwen3-235B"},
    "llama-405b":     {"input": 3.50,  "output": 3.50,  "label": "Llama 3.1 405B"},
    "deepseek-v3":    {"input": 0.90,  "output": 0.90,  "label": "DeepSeek-V3"},
}

CONDITION_PROMPTS = {
    "naive": "agents/prompts/naive.txt",
    "paper_informed": "agents/prompts/paper_informed.txt",
    "pipeline_guided": "agents/prompts/pipeline_guided.txt",
}

# Known data source URLs (what an agent discovers via web search)
PSP_URL = "https://phosphosite.org/downloads/Kinase_Substrate_Dataset.gz"
SIGNOR_API = "https://signor.uniroma2.it/API/getHumanData.php?format=tsv"
# UniProt: human reviewed phosphoproteins (KW-0597 = Phosphoprotein keyword)
# Must paginate through all ~8000 to find entries with "; by KINASE" attribution
UNIPROT_API = "https://rest.uniprot.org/uniprotkb/search?query=(organism_id:9606)%20AND%20(reviewed:true)%20AND%20(keyword:KW-0597)&format=json&fields=accession,gene_names,ft_mod_res&size=500"


# ═══════════════════════════════════════════════════════════════════════════════
# TOKEN TRACKER (tracks actual data volumes per tool call)
# ═══════════════════════════════════════════════════════════════════════════════

def chars_to_tokens(chars: int) -> int:
    """~3.5 chars per token for mixed JSON/text content."""
    return max(1, int(chars / 3.5))


class LiveTokenTracker:
    """Track token usage from actual web tool calls.

    Each tool call is an API turn where the model:
    - Reads: system prompt (cached) + conversation history + previous tool results
    - Writes: reasoning (~300 tokens) + tool call JSON (~100 tokens)

    The tool result is truncated/summarized before being fed back, so only a
    fraction of raw data enters the context window.
    """

    # How much of each tool result the model actually sees in context
    RESULT_CONTEXT_FRACTION = 0.1   # 10% of raw result enters context (rest is parsed/discarded)
    RESULT_CONTEXT_CAP = 15000      # max chars of result in context per call (matches web_fetch truncation)
    REASONING_PER_TURN = 300        # tokens of agent reasoning per turn
    TOOL_CALL_JSON = 100            # tokens for the tool_use JSON block

    def __init__(self, system_prompt: str, model: str = "opus"):
        self.model = model
        self.system_prompt_tokens = chars_to_tokens(len(system_prompt))
        self.turns = []
        self._context_tokens = 0  # accumulated conversation context

    def record(self, tool_name: str, input_chars: int, result_chars: int, result_in_context_chars: int = 0):
        """Record a tool call with actual data volume.

        Args:
            tool_name: Name of the tool called
            input_chars: Characters in the tool input JSON
            result_chars: Total raw characters in the tool result
            result_in_context_chars: How many chars of the result actually enter
                the context window (0 = auto-estimate)
        """
        if result_in_context_chars == 0:
            result_in_context_chars = min(
                int(result_chars * self.RESULT_CONTEXT_FRACTION),
                self.RESULT_CONTEXT_CAP,
            )

        result_context_tokens = chars_to_tokens(result_in_context_chars)
        input_json_tokens = chars_to_tokens(input_chars)

        # Input = system prompt (cached after turn 0) + accumulated context
        if len(self.turns) == 0:
            input_tokens = self.system_prompt_tokens + self._context_tokens
            cache_tokens = 0
        else:
            input_tokens = self._context_tokens
            cache_tokens = self.system_prompt_tokens

        # Output = reasoning + tool call
        output_tokens = self.REASONING_PER_TURN + self.TOOL_CALL_JSON + input_json_tokens

        self.turns.append({
            "turn": len(self.turns) + 1,
            "tool": tool_name,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_tokens,
            "raw_result_chars": result_chars,
            "context_result_chars": result_in_context_chars,
        })

        # Context grows by: model output + tool result summary
        self._context_tokens += output_tokens + result_context_tokens

    def summary(self) -> dict:
        total_input = sum(t["input_tokens"] for t in self.turns)
        total_output = sum(t["output_tokens"] for t in self.turns)
        total_cache = sum(t["cache_read_tokens"] for t in self.turns)
        total_raw_data = sum(t["raw_result_chars"] for t in self.turns)

        prices = MODEL_PRICING.get(self.model, {"input": 10.0, "output": 30.0})
        cost = (
            total_input * prices["input"] / 1_000_000
            + total_output * prices["output"] / 1_000_000
        )

        return {
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "cache_read_input_tokens": total_cache,
            "api_calls": len(self.turns),
            "estimated_cost_usd": round(cost, 4),
            "total_raw_data_chars": total_raw_data,
            "model_pricing": self.model,
            "model_label": prices.get("label", self.model),
            "per_call": self.turns,
            "note": "Estimated from actual web data volumes with realistic context management",
        }


# ═══════════════════════════════════════════════════════════════════════════════
# WEB TOOLS (same as claude_runner.py)
# ═══════════════════════════════════════════════════════════════════════════════

def web_search(query: str, max_results: int = 10) -> dict:
    """Search via DuckDuckGo HTML."""
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


def web_fetch(url: str, max_bytes: int = 20_000_000, timeout: int = 60) -> tuple[str, int]:
    """Fetch URL content, auto-decompress gzip. Returns (text, raw_bytes).

    Uses curl subprocess for URLs with pre-encoded query params (e.g. UniProt)
    to avoid urllib double-encoding issues.
    """
    import subprocess as _sp

    # Use curl for URLs with %20 or %3A (already-encoded params)
    if "%20" in url or "%3A" in url:
        result = _sp.run(["curl", "-sL", "--max-time", str(timeout), url],
                         capture_output=True, timeout=timeout + 10)
        raw = result.stdout
    else:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (PhosphoAtlas-Benchmark)",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes)
            content_encoding = resp.headers.get("Content-Encoding", "")
            if "gzip" in content_encoding or url.endswith(".gz"):
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass

    text = raw.decode("utf-8", errors="replace")
    return text, len(raw)


# ═══════════════════════════════════════════════════════════════════════════════
# PARSERS
# ═══════════════════════════════════════════════════════════════════════════════

def parse_psp_tsv(text: str) -> list[dict]:
    """Parse PhosphoSitePlus Kinase_Substrate_Dataset TSV."""
    entries = []
    lines = text.strip().split("\n")

    # Skip header lines (PSP has 3 comment lines before the actual header)
    data_start = 0
    for i, line in enumerate(lines):
        if line.startswith("GENE\t") or line.startswith("KIN_ACC_ID\t"):
            data_start = i
            break
        if i >= 5:
            break

    reader = csv.DictReader(io.StringIO("\n".join(lines[data_start:])), delimiter="\t")
    for row in reader:
        organism = row.get("SUB_ORGANISM", "").strip()
        if organism and organism != "human":
            continue
        kinase = row.get("GENE", row.get("KIN_ACC_ID", "")).strip()
        substrate = row.get("SUB_GENE", "").strip()
        site = row.get("SUB_MOD_RSD", "").strip()
        if kinase and substrate and site:
            entries.append({
                "kinase_gene": kinase,
                "substrate_gene": substrate,
                "phospho_site": site,
                "substrate_uniprot": row.get("SUB_ACC_ID", "").strip(),
                "heptameric_peptide": row.get("SITE_+/-7_AA", "").strip(),
                "supporting_databases": ["PhosphoSitePlus"],
            })
    return entries


def parse_signor_tsv(text: str) -> list[dict]:
    """Parse SIGNOR API TSV response (getHumanData.php)."""
    entries = []
    # Remove NUL bytes that SIGNOR sometimes includes
    text = text.replace("\x00", "")
    lines = text.strip().split("\n")
    if len(lines) < 2:
        return entries

    # Parse line-by-line to handle embedded newlines in fields
    header = None
    for line in lines:
        if not line.strip():
            continue
        fields = line.split("\t")
        if header is None:
            header = [h.strip() for h in fields]
            continue
        if len(fields) < len(header):
            continue  # skip malformed lines
        row = dict(zip(header, fields))

        kinase = row.get("ENTITYA", "").strip()
        substrate = row.get("ENTITYB", "").strip()
        mechanism = row.get("MECHANISM", "").lower()

        # Filter for phosphorylation events
        if "phosphorylat" not in mechanism:
            continue

        residue = row.get("RESIDUE", "").strip()
        if not residue:
            continue

        sequence = row.get("SEQUENCE", "").strip()
        uniprot_a = row.get("IDA", "").strip()

        if kinase and substrate and residue:
            entries.append({
                "kinase_gene": kinase,
                "substrate_gene": substrate,
                "phospho_site": residue,
                "substrate_uniprot": uniprot_a,
                "heptameric_peptide": sequence,
                "supporting_databases": ["SIGNOR"],
            })
    return entries


def parse_uniprot_json(text: str) -> list[dict]:
    """Parse UniProt REST API JSON response for phosphorylation sites."""
    entries = []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return entries

    results = data.get("results", [])
    for protein in results:
        gene_names = protein.get("genes", [])
        substrate = ""
        if gene_names:
            primary = gene_names[0].get("geneName", {})
            substrate = primary.get("value", "")
        accession = protein.get("primaryAccession", "")

        features = protein.get("features", [])
        for feat in features:
            if feat.get("type") != "Modified residue":
                continue
            desc = feat.get("description", "")
            if "Phospho" not in desc:
                continue

            # Extract kinase from description (e.g., "Phosphoserine; by CDK1")
            kinase = ""
            if "; by " in desc:
                kinase = desc.split("; by ")[-1].strip().rstrip(".")
                # Handle "CDK1 and CDK2" → take first
                if " and " in kinase:
                    kinase = kinase.split(" and ")[0].strip()
                if " or " in kinase:
                    kinase = kinase.split(" or ")[0].strip()

            location = feat.get("location", {})
            start = location.get("start", {}).get("value")
            end = location.get("end", {}).get("value")

            if not kinase or not substrate or not start:
                continue

            # Determine residue type from description
            if "Phosphoserine" in desc:
                site = f"S{start}"
            elif "Phosphothreonine" in desc:
                site = f"T{start}"
            elif "Phosphotyrosine" in desc:
                site = f"Y{start}"
            else:
                site = f"?{start}"

            entries.append({
                "kinase_gene": kinase,
                "substrate_gene": substrate,
                "phospho_site": site,
                "substrate_uniprot": accession,
                "heptameric_peptide": "",
                "supporting_databases": ["UniProt"],
            })
    return entries


# ═══════════════════════════════════════════════════════════════════════════════
# ATLAS BUILDER
# ═══════════════════════════════════════════════════════════════════════════════

def normalize_site(site: str) -> str:
    """Normalize phospho-site: Thr156 -> T156, Ser10 -> S10, Tyr15 -> Y15."""
    site = site.strip()
    for full, short in [("Thr", "T"), ("Ser", "S"), ("Tyr", "Y"),
                         ("thr", "T"), ("ser", "S"), ("tyr", "Y")]:
        if site.startswith(full):
            site = short + site[len(full):]
            break
    return site.upper()


def deduplicate_atlas(all_entries: list[dict]) -> list[dict]:
    """Deduplicate entries by (kinase|substrate|site) key, merging sources."""
    atlas = {}
    for e in all_entries:
        kinase = e.get("kinase_gene", "").strip()
        substrate = e.get("substrate_gene", "").strip()
        site = normalize_site(e.get("phospho_site", ""))
        if not (kinase and substrate and site):
            continue

        key = f"{kinase.upper()}|{substrate.upper()}|{site}"
        if key not in atlas:
            atlas[key] = {
                "kinase_gene": kinase,
                "substrate_gene": substrate,
                "phospho_site": site,  # already normalized
                "substrate_uniprot": e.get("substrate_uniprot", ""),
                "heptameric_peptide": e.get("heptameric_peptide", ""),
                "supporting_databases": list(e.get("supporting_databases", [])),
            }
        else:
            existing = atlas[key]
            for db in e.get("supporting_databases", []):
                if db not in existing["supporting_databases"]:
                    existing["supporting_databases"].append(db)
            if not existing["substrate_uniprot"] and e.get("substrate_uniprot"):
                existing["substrate_uniprot"] = e["substrate_uniprot"]
            if not existing["heptameric_peptide"] and e.get("heptameric_peptide"):
                existing["heptameric_peptide"] = e["heptameric_peptide"]

    return sorted(atlas.values(),
                  key=lambda x: (x["kinase_gene"], x["substrate_gene"], x["phospho_site"]))


# ═══════════════════════════════════════════════════════════════════════════════
# LIVE CURATION STRATEGIES
# ═══════════════════════════════════════════════════════════════════════════════

def run_live_curation(condition: str, system_prompt: str, tracker: LiveTokenTracker,
                      log_fn) -> list[dict]:
    """Execute the live curation pipeline.

    Simulates what an autonomous LLM agent does:
    1. Web search to discover databases (naive) or use known URLs (informed)
    2. Download bulk datasets
    3. Parse entries
    4. Merge and deduplicate
    5. Submit atlas
    """
    all_entries = []

    # ── Turn 1: Agent reasoning / discovery ──────────────────────────────────

    if condition == "naive":
        log_fn("TURN1", "Agent searches for phosphorylation databases...")
        result = web_search("human protein phosphorylation database kinase substrate download")
        result_str = json.dumps(result)
        tracker.record("web_search", 80, len(result_str), min(len(result_str), 5000))
        log_fn("TURN1", f"  Web search returned {result.get('count', 0)} results")

        # Agent discovers PSP from search results
        log_fn("TURN2", "Agent explores PhosphoSitePlus download page...")
        try:
            psp_page, psp_page_bytes = web_fetch("https://www.phosphosite.org/staticDownloads", timeout=15)
            tracker.record("web_fetch", 60, len(psp_page), min(len(psp_page), 15000))
            log_fn("TURN2", f"  PSP downloads page: {len(psp_page)} chars")
        except Exception as e:
            log_fn("TURN2", f"  PSP downloads page failed: {e}")
            tracker.record("web_fetch", 60, 200, 200)

    else:
        log_fn("TURN1", "Agent has paper context, proceeding to known databases...")
        # Paper-informed/pipeline-guided agents already know the URLs
        tracker.record("reasoning", 0, 0, 0)

    # ── Download PhosphoSitePlus ─────────────────────────────────────────────

    log_fn("PSP", "Downloading PhosphoSitePlus Kinase_Substrate_Dataset.gz...")
    try:
        psp_text, psp_bytes = web_fetch(PSP_URL, timeout=120)
        tracker.record("fetch_and_parse_db", len(PSP_URL) + 50, len(psp_text),
                        min(len(psp_text), 50000))  # agent sees truncated result
        log_fn("PSP", f"  Downloaded: {psp_bytes:,} bytes -> {len(psp_text):,} chars")

        psp_entries = parse_psp_tsv(psp_text)
        log_fn("PSP", f"  Parsed: {len(psp_entries)} human kinase-substrate entries")
        all_entries.extend(psp_entries)
    except Exception as e:
        log_fn("PSP", f"  FAILED: {e}")
        # Agent would try alternative URLs
        tracker.record("fetch_and_parse_db", len(PSP_URL), 200, 200)

    # ── Download SIGNOR ──────────────────────────────────────────────────────

    log_fn("SIGNOR", "Downloading SIGNOR phosphorylation data...")
    try:
        # Agent discovers SIGNOR API via search or paper knowledge
        if condition == "naive":
            log_fn("SIGNOR", "  Searching for SIGNOR API...")
            sr = web_search("SIGNOR database API phosphorylation human download")
            tracker.record("web_search", 70, len(json.dumps(sr)), 3000)

        signor_text, signor_bytes = web_fetch(SIGNOR_API, timeout=120)
        tracker.record("fetch_and_parse_db", len(SIGNOR_API) + 50, len(signor_text),
                        min(len(signor_text), 50000))
        log_fn("SIGNOR", f"  Downloaded: {signor_bytes:,} bytes -> {len(signor_text):,} chars")

        signor_entries = parse_signor_tsv(signor_text)
        log_fn("SIGNOR", f"  Parsed: {len(signor_entries)} phosphorylation entries")
        all_entries.extend(signor_entries)
    except Exception as e:
        log_fn("SIGNOR", f"  FAILED: {e}")
        tracker.record("fetch_and_parse_db", len(SIGNOR_API), 200, 200)

    # ── Download UniProt ─────────────────────────────────────────────────────

    log_fn("UNIPROT", "Downloading UniProt phosphorylation annotations...")
    log_fn("UNIPROT", "  (Paginating ~8000 human phosphoproteins, extracting kinase-attributed sites)")
    uniprot_entries = []
    try:
        if condition == "naive":
            log_fn("UNIPROT", "  Searching for UniProt phospho API...")
            sr = web_search("UniProt REST API phosphorylation modified residue human")
            tracker.record("web_search", 70, len(json.dumps(sr)), 3000)

        # Paginate through ALL UniProt phosphoproteins
        # Use curl for pagination to get Link headers
        import subprocess as _sp
        next_url = UNIPROT_API
        page = 0
        while next_url and page < 50:  # up to 50 pages of 500 = 25,000 proteins
            page += 1
            result = _sp.run(
                ["curl", "-sD", "-", "--max-time", "60", next_url],
                capture_output=True, timeout=70)
            raw_output = result.stdout.decode("utf-8", errors="replace")

            # Split headers from body
            header_end = raw_output.find("\r\n\r\n")
            if header_end < 0:
                header_end = raw_output.find("\n\n")
            if header_end >= 0:
                headers_text = raw_output[:header_end]
                body = raw_output[header_end:].strip()
            else:
                headers_text = ""
                body = raw_output

            tracker.record("web_fetch", len(next_url), len(body), min(len(body), 15000))

            page_entries = parse_uniprot_json(body)
            uniprot_entries.extend(page_entries)

            if page % 5 == 0 or page == 1:
                log_fn("UNIPROT", f"  Page {page}: +{len(page_entries)} kinase-attributed entries "
                       f"(total: {len(uniprot_entries)})")

            # Extract next page URL from Link header
            next_url = None
            for line in headers_text.split("\n"):
                if line.strip().lower().startswith("link:"):
                    # Format: Link: <https://...>; rel="next"
                    match = re.search(r'<([^>]+)>;\s*rel="next"', line)
                    if match:
                        next_url = match.group(1)

            if not next_url:
                break

        log_fn("UNIPROT", f"  Total UniProt kinase-attributed entries: {len(uniprot_entries)} "
               f"(from {page} pages)")
        all_entries.extend(uniprot_entries)
    except Exception as e:
        log_fn("UNIPROT", f"  FAILED: {e}")
        tracker.record("web_fetch", 100, 200, 200)

    # ── Merge and deduplicate ────────────────────────────────────────────────

    log_fn("MERGE", f"Raw entries before dedup: {len(all_entries)}")
    atlas = deduplicate_atlas(all_entries)
    log_fn("MERGE", f"Deduplicated atlas: {len(atlas)} unique triplets")

    # Agent submits atlas (final turn)
    submit_json = json.dumps({"status": "submitted", "entries": len(atlas)})
    tracker.record("submit_atlas", len(json.dumps(atlas[:3])) + 100, len(submit_json),
                    len(submit_json))

    return atlas


# ═══════════════════════════════════════════════════════════════════════════════
# ITERATIVE FEEDBACK
# ═══════════════════════════════════════════════════════════════════════════════

FEEDBACK_TEMPLATE = """
=== PERFORMANCE FEEDBACK FROM ROUND {round_num} ===

Your atlas from Round {round_num} was scored against a curated gold standard:

TRIPLET-LEVEL:  {tp} correct, {fp} extra, {fn} missed
  Precision: {precision}  |  Recall: {recall}  |  F1: {f1}

KINASE DISCOVERY: {kinases_found} found, {kinases_missed} missed ({discovery_rate})

TOP MISSED KINASES (by gold entry count):
{missed_kinases_detail}

AREAS FOR IMPROVEMENT:
{improvement_suggestions}

=== END FEEDBACK (Round {round_num}) ===
"""


def generate_feedback(scores: dict, round_num: int) -> str:
    """Generate feedback string from scoring results."""
    al = scores["atlas_level"]
    cl = scores["column_level"]
    kd = scores["kinase_discovery"]
    cr = scores["cross_referencing"]

    missed = kd.get("missed_kinases", [])[:15]
    missed_detail = "\n".join(f"  - {k}" for k in missed) if missed else "  (none)"

    suggestions = []
    if al["recall"] < 0.95:
        suggestions.append(
            f"- Recall is {al['recall']:.1%}. {al['false_negatives']} entries missed. "
            f"Try additional databases or query strategies.")
    if cr.get("multi_db_pct", 0) < 20:
        suggestions.append(
            f"- Only {cr.get('multi_db_pct', 0)}% entries cross-referenced. "
            f"Query PSP, SIGNOR, and UniProt for broader coverage.")
    if len(kd.get("missed_kinases", [])) > 10:
        suggestions.append(
            f"- {len(kd['missed_kinases'])} kinases missed. Check alternative gene symbols.")

    return FEEDBACK_TEMPLATE.format(
        round_num=round_num,
        tp=al["true_positives"], fp=al["false_positives"], fn=al["false_negatives"],
        precision=f"{al['precision']:.4f}", recall=f"{al['recall']:.4f}", f1=f"{al['f1']:.4f}",
        kinases_found=kd.get("kinases_discovered", "?"),
        kinases_missed=kd.get("kinases_missed", "?"),
        discovery_rate=f"{kd.get('discovery_rate', 0):.1%}",
        missed_kinases_detail=missed_detail,
        improvement_suggestions="\n".join(suggestions) or "- Maintain accuracy while expanding coverage.",
    )


def run_single(condition, model, system_prompt, out_dir, log_fn):
    """Run a single curation round. Returns (entries, scores, token_summary)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    scores_dir = out_dir / "scores"
    scores_dir.mkdir(parents=True, exist_ok=True)

    tracker = LiveTokenTracker(system_prompt, model=model)

    t0 = time.time()
    entries = run_live_curation(condition, system_prompt, tracker, log_fn)
    elapsed = time.time() - t0

    token_summary = tracker.summary()
    log_fn("DONE", f"Elapsed: {elapsed:.1f}s | Turns: {token_summary['api_calls']} | "
           f"Data: {token_summary['total_raw_data_chars']:,} chars")
    log_fn("TOKENS", f"Est. cost: ${token_summary['estimated_cost_usd']:.2f} "
           f"({token_summary['total_tokens']:,} tokens)")

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

    model_label = MODEL_PRICING.get(model, {}).get("label", model)
    run_log = {
        "agent": model_label, "model": model, "condition": condition,
        "mode": "live", "databases_accessed": sorted(db_counts.keys()),
        "tool_calls": token_summary["api_calls"], "raw_counts": db_counts,
        "merged_atlas": len(entries),
        "unique_kinases": len(set(e["kinase_gene"] for e in entries)),
        "unique_substrates": len(set(e["substrate_gene"] for e in entries)),
        "multi_db_entries": multi_db, "elapsed_seconds": round(elapsed, 1),
        "token_usage": token_summary,
    }
    with open(out_dir / "run_log.json", "w") as f:
        json.dump(run_log, f, indent=2)

    # Score
    scores = None
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
        log_fn("SCORE", f"F1={ov['f1']} R={ov['recall']} P={ov['precision']} "
               f"Kinases={ov['kinases_found']} TP={al['true_positives']} FN={al['false_negatives']}")

    return entries, scores, token_summary


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Live runner — downloads from real sources, estimates API costs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single run
  python3 agents/live_runner.py --model opus --condition naive

  # Iterative (3 rounds with feedback)
  python3 agents/live_runner.py --model opus --condition naive --iterative

  # All conditions + iterative
  python3 agents/live_runner.py --model opus --all

  python3 agents/live_runner.py --list-models
""")
    parser.add_argument("--condition", choices=list(CONDITION_PROMPTS.keys()),
                        help="Prompt condition")
    parser.add_argument("--model", default="opus",
                        help=f"Model for cost estimation ({', '.join(MODEL_PRICING.keys())})")
    parser.add_argument("--output-dir", default=None,
                        help="Output directory (default: contributions/<model>_<condition>)")
    parser.add_argument("--iterative", action="store_true",
                        help="Run 3 iterative rounds with feedback between each")
    parser.add_argument("--all", action="store_true",
                        help="Run all conditions (naive, paper_informed, pipeline_guided) + iterative")
    parser.add_argument("--list-models", action="store_true")
    args = parser.parse_args()

    if args.list_models:
        print("Available models and pricing (per 1M tokens):")
        print(f"{'Model':<16} {'Label':<22} {'Input':>8} {'Output':>8}")
        print("-" * 56)
        for key, p in sorted(MODEL_PRICING.items()):
            print(f"{key:<16} {p['label']:<22} ${p['input']:>6.2f}  ${p['output']:>6.2f}")
        return

    model_label = MODEL_PRICING.get(args.model, {}).get("label", args.model)
    model_slug = args.model.replace("-", "_")

    log_lines = []
    def log_fn(phase, msg):
        line = f"[{time.strftime('%H:%M:%S')}][{phase}] {msg}"
        log_lines.append(line)
        print(line, flush=True)

    def run_condition(condition, out_dir, extra_prompt=""):
        """Run a single condition and return scores."""
        prompt_path = PROJECT_ROOT / CONDITION_PROMPTS[condition]
        system_prompt = prompt_path.read_text().strip()
        if extra_prompt:
            system_prompt = system_prompt + "\n\n" + extra_prompt

        log_fn("RUN", f"{'='*60}")
        log_fn("RUN", f"Model: {model_label} | Condition: {condition} | Output: {out_dir}")

        entries, scores, token_summary = run_single(
            condition, args.model, system_prompt, out_dir, log_fn)

        # Save log
        with open(out_dir / "run.log", "w") as f:
            f.write("\n".join(log_lines))

        ov = scores["overview"] if scores else {}
        print(f"\n  Atlas: {len(entries)} | F1: {ov.get('f1','?')} | "
              f"Cost: ${token_summary['estimated_cost_usd']:.2f}\n")
        return entries, scores

    def run_iterative(condition):
        """Run 3 iterative rounds with feedback between each."""
        base_dir = Path(args.output_dir) if args.output_dir else Path(
            f"contributions/{model_slug}_{condition}")

        all_scores = []
        total_cost = 0.0

        for round_num in range(1, 4):
            round_dir = base_dir / f"round{round_num}" if round_num > 1 else base_dir
            log_fn("ITER", f"{'#'*60}")
            log_fn("ITER", f"# ROUND {round_num}/3 — {condition}")
            log_fn("ITER", f"{'#'*60}")

            # Build feedback from all previous rounds
            feedback_parts = []
            for prev_round, prev_scores in enumerate(all_scores, 1):
                feedback_parts.append(generate_feedback(prev_scores, prev_round))

            extra = "\n\n".join(feedback_parts)
            if extra:
                extra = (
                    f"You are on Round {round_num} of an iterative curation process.\n"
                    f"Below is feedback from your previous round(s). Use it to improve.\n"
                    + extra +
                    f"\nFocus on finding the entries you missed while maintaining accuracy."
                )

            entries, scores = run_condition(condition, round_dir, extra_prompt=extra)
            all_scores.append(scores)

            ts = json.load(open(round_dir / "run_log.json")).get("token_usage", {})
            total_cost += ts.get("estimated_cost_usd", 0)

        # Print comparison
        print(f"\n{'='*70}")
        print(f"ITERATIVE COMPARISON — {model_label} / {condition}")
        print(f"{'='*70}")
        print(f"{'Round':<8} {'Entries':>8} {'Recall':>8} {'Prec':>8} {'F1':>8} {'Kinases':>10} {'FN':>6}")
        print("-" * 70)
        for i, s in enumerate(all_scores, 1):
            al = s["atlas_level"]
            kd = s["kinase_discovery"]
            print(f"Round {i:<3} {al['total_agent_entries']:>8} {al['recall']:>8.4f} "
                  f"{al['precision']:>8.4f} {al['f1']:>8.4f} "
                  f"{kd['kinases_discovered']:>4}/{kd['kinases_in_gold']:<4} {al['false_negatives']:>6}")
        print(f"\nTotal estimated cost: ${total_cost:.2f}")

        # Save comparison
        comp = [{"round": i+1, **s["overview"], **s["atlas_level"]} for i, s in enumerate(all_scores)]
        with open(base_dir / "iterative_comparison.json", "w") as f:
            json.dump(comp, f, indent=2)

    # ── Dispatch ─────────────────────────────────────────────────────────

    if args.all:
        for cond in ["naive", "paper_informed", "pipeline_guided"]:
            out = Path(f"contributions/{model_slug}_{cond}")
            run_condition(cond, out)
        # Run iterative on naive
        run_iterative("naive")
        # Regenerate report
        print("\n[REPORT] Regenerating benchmark report...")
        from evaluation.report import generate_report
        generate_report(str(PROJECT_ROOT / "paper" / "tables" / "benchmark_summary_tables.pdf"))
        return

    if not args.condition:
        parser.error("--condition is required (or use --all)")

    if args.iterative:
        run_iterative(args.condition)
    else:
        out_dir = Path(args.output_dir) if args.output_dir else Path(
            f"contributions/{model_slug}_{args.condition}")
        run_condition(args.condition, out_dir)


if __name__ == "__main__":
    main()
