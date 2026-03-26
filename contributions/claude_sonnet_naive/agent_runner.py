#!/usr/bin/env python3
"""
Claude Sonnet 4.6 Naive Agent Runner — Autonomous LLM-Driven Discovery.

Runs Claude Sonnet as a genuine autonomous agent via the Anthropic API.
The agent receives only the naive system prompt and a set of tools.
It independently decides which databases to query, discovers that local
data is empty, searches the web for public API endpoints, downloads and
parses data, and submits the atlas — all through its own reasoning.

Tools available to the agent:
  Database tools (from DatabaseTools — return empty without local files):
    list_databases, get_stats, list_kinases, list_substrates,
    query_by_kinase, query_by_substrate, query_by_site, query_all_dbs, search

  Web tools (for independent discovery when local data is absent):
    web_search          — search the web via DuckDuckGo
    web_fetch           — fetch a web page (truncated to 15 000 chars)
    fetch_and_parse_db  — download a URL, auto-detect format, parse phospho
                          entries, and accumulate them internally

  Submission:
    submit_atlas        — finalize and submit all accumulated entries

Requires: ANTHROPIC_API_KEY environment variable.

Usage:
  export ANTHROPIC_API_KEY=sk-ant-...
  python3 contributions/claude_sonnet_naive/agent_runner.py
"""
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from databases.tools import DatabaseTools

try:
    import anthropic
except ImportError:
    sys.exit("Error: pip install anthropic")


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY ACCUMULATOR — stores parsed entries across multiple fetch calls
# ═══════════════════════════════════════════════════════════════════════════════

class EntryAccumulator:
    """Deduplicating accumulator keyed by (kinase|substrate|site)."""

    def __init__(self):
        self._atlas = {}

    def add(self, kinase, substrate, site, uniprot="", peptide="", source=""):
        if not (kinase and substrate and site):
            return
        key = f"{kinase}|{substrate}|{site}"
        if key not in self._atlas:
            self._atlas[key] = {
                "kinase_gene": kinase,
                "substrate_gene": substrate,
                "phospho_site": site,
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

    def size(self):
        return len(self._atlas)

    def finalize(self):
        """Return sorted entry list."""
        return sorted(
            self._atlas.values(),
            key=lambda e: (e["kinase_gene"], e["substrate_gene"], e["phospho_site"]),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL IMPLEMENTATIONS
# ═══════════════════════════════════════════════════════════════════════════════

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _http_get(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    return urllib.request.urlopen(req, timeout=timeout)


def tool_web_search(query: str) -> dict:
    """Search DuckDuckGo and return result URLs with snippets."""
    search_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    try:
        req = urllib.request.Request(search_url, headers={"User-Agent": _UA})
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="replace")
        # Extract redirect URLs
        redirects = re.findall(r"uddg=(https?%3A[^&\"]+)", html)
        urls = [urllib.parse.unquote(u) for u in redirects]
        if not urls:
            urls = re.findall(
                r'href="(https?://(?!duckduckgo|html\.duckduckgo|improving)[^"]+)"', html
            )
        # Deduplicate
        seen = set()
        unique = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                unique.append(u)
        return {"query": query, "results": unique[:15]}
    except Exception as e:
        return {"query": query, "error": str(e), "results": []}


def tool_web_fetch(url: str, max_chars: int = 15000) -> dict:
    """Fetch a URL and return its content (truncated)."""
    try:
        resp = _http_get(url, timeout=30)
        ct = (resp.headers.get("Content-Type") or "").lower()
        raw = resp.read()

        # Gzip
        if "gzip" in ct or url.endswith(".gz"):
            try:
                text = gzip.decompress(raw).decode("utf-8", errors="replace")
            except Exception:
                text = raw.decode("utf-8", errors="replace")
        else:
            text = raw.decode("utf-8", errors="replace")

        truncated = len(text) > max_chars
        return {
            "url": url,
            "content_type": ct,
            "size_bytes": len(raw),
            "content": text[:max_chars],
            "truncated": truncated,
        }
    except Exception as e:
        return {"url": url, "error": str(e)}


# ── Phosphorylation data parsers (auto-detect format) ─────────────────────────

_RESIDUE_MAP = {
    "phosphoserine": "S", "phosphothreonine": "T",
    "phosphotyrosine": "Y", "phosphohistidine": "H",
}


def _parse_gzipped_tsv(raw_bytes, db_name):
    """Parse gzipped TSV (PSP-style)."""
    text = gzip.decompress(raw_bytes).decode("utf-8", errors="replace")
    lines = text.split("\n")
    # Find header row (first line with 5+ tabs)
    header_idx = 0
    for i, line in enumerate(lines):
        if line.count("\t") >= 5:
            header_idx = i
            break
    headers = lines[header_idx].split("\t")
    # Map columns by priority
    col_priority = {
        "kinase": ["GENE", "KINASE_GENE", "ENTITYA"],
        "substrate": ["SUB_GENE", "SUBSTRATE_GENE", "ENTITYB"],
        "site": ["SUB_MOD_RSD", "PHOSPHO_SITE", "RESIDUE"],
        "uniprot": ["SUB_ACC_ID", "SUBSTRATE_UNIPROT"],
        "peptide": ["SITE_+/-7_AA", "SEQUENCE"],
        "organism": ["SUB_ORGANISM", "ORGANISM", "TAX_ID"],
    }
    hdr_idx = {h.strip(): i for i, h in enumerate(headers)}
    col_map = {}
    for field, aliases in col_priority.items():
        for alias in aliases:
            if alias in hdr_idx:
                col_map[field] = hdr_idx[alias]
                break
    entries = []
    for line in lines[header_idx + 1:]:
        row = line.split("\t")
        if len(row) <= max(col_map.values(), default=0):
            continue
        if "organism" in col_map:
            org = row[col_map["organism"]].strip()
            if org and org != "human" and org != "9606":
                continue
        k = row[col_map.get("kinase", -1)].strip() if "kinase" in col_map else ""
        s = row[col_map.get("substrate", -1)].strip() if "substrate" in col_map else ""
        site = row[col_map.get("site", -1)].strip() if "site" in col_map else ""
        if k and s and site:
            entries.append({
                "kinase_gene": k, "substrate_gene": s, "phospho_site": site,
                "substrate_uniprot": row[col_map.get("uniprot", -1)].strip() if "uniprot" in col_map else "",
                "heptameric_peptide": row[col_map.get("peptide", -1)].strip() if "peptide" in col_map else "",
            })
    return entries


def _parse_plain_tsv(text, db_name):
    """Parse plain TSV — headerless (SIGNOR-style) or headed."""
    lines = text.strip().split("\n")
    if not lines:
        return []
    # Check if col 9 looks like a mechanism column (SIGNOR layout)
    mechs = set()
    for line in lines[:100]:
        cols = line.split("\t")
        if len(cols) > 9:
            mechs.add(cols[9].strip().lower())
    if "phosphorylation" in mechs:
        entries = []
        for line in lines:
            cols = line.split("\t")
            if len(cols) < 12:
                continue
            if cols[9].strip().lower() != "phosphorylation":
                continue
            if cols[1].strip() != "protein" or cols[5].strip() != "protein":
                continue
            site = cols[10].strip()
            if not site:
                continue
            k, s = cols[0].strip(), cols[4].strip()
            if k and s:
                entries.append({
                    "kinase_gene": k, "substrate_gene": s, "phospho_site": site,
                    "heptameric_peptide": cols[11].strip() if len(cols) > 11 else "",
                })
        return entries
    return []


def _parse_uniprot_json(raw_bytes):
    """Parse UniProt JSON — handles paginated protein records."""
    data = json.loads(raw_bytes)
    results = data.get("results", []) if isinstance(data, dict) else data
    entries = []
    for protein in results:
        acc = protein.get("primaryAccession", "")
        genes = protein.get("genes", [])
        sub = ""
        if genes:
            gn = genes[0].get("geneName", {})
            sub = gn.get("value", "") if isinstance(gn, dict) else ""
        if not sub:
            continue
        for feat in protein.get("features", []):
            if feat.get("type") != "Modified residue":
                continue
            desc = feat.get("description", "")
            dl = desc.lower()
            if "phospho" not in dl:
                continue
            by_m = re.search(r"\bby\s+(\S+)", desc)
            if not by_m:
                continue
            by_section = desc[by_m.start():]
            kinases = re.findall(r"\b([A-Z][A-Z0-9]{1,})\b", by_section)
            if not kinases:
                kinases = [by_m.group(1)]
            residue = ""
            for key, code in _RESIDUE_MAP.items():
                if key in dl:
                    residue = code
                    break
            if not residue:
                continue
            loc = feat.get("location", {})
            pos = loc.get("start", {})
            position = pos.get("value", "") if isinstance(pos, dict) else ""
            if not position:
                continue
            site = f"{residue}{position}"
            skip = {"AND", "OR", "THE", "IN", "VITRO", "VIVO", "NOT"}
            for kin in kinases:
                if kin not in skip:
                    entries.append({
                        "kinase_gene": kin, "substrate_gene": sub,
                        "substrate_uniprot": acc, "phospho_site": site,
                    })
    return entries


def _parse_generic_json(raw_bytes, db_name):
    """Parse JSON that is NOT UniProt protein records — handles many field-name variants.

    Covers: PhosphoSIGNOR API, flat JSON exports, and other formats.
    PhosphoSIGNOR uses entityA_name/entityB_name with embedded site (e.g. 'TOP2A_phSer1247').
    """
    try:
        data = json.loads(raw_bytes)
    except json.JSONDecodeError:
        return []
    records = data if isinstance(data, list) else data.get("results", data.get("entries", []))
    if not isinstance(records, list):
        return []

    _k = ("kinase_gene", "kinase", "KINASE", "kinaseName", "entityA_name",
          "ENTITYA", "kinase_name")
    _s = ("substrate_gene", "substrate", "SUBSTRATE", "substrateName",
          "entityB_name", "ENTITYB", "substrate_name")
    _site = ("phospho_site", "site", "SITE", "phosphosite", "residue",
             "RESIDUE", "modification")
    _up = ("substrate_uniprot", "uniprot", "accession", "SUB_ACC_ID")
    _pep = ("heptameric_peptide", "peptide", "sequence", "SEQUENCE", "SITE_+/-7_AA")

    def _pick(rec, keys):
        for k in keys:
            v = rec.get(k)
            if v:
                return str(v).strip()
        return ""

    entries = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        kinase = _pick(rec, _k)
        substrate = _pick(rec, _s)
        site = _pick(rec, _site)

        # PhosphoSIGNOR encodes site in entityB_name like "TOP2A_phSer1247"
        if substrate and not site and "_ph" in substrate:
            m = re.match(r"^(.+?)_ph(Ser|Thr|Tyr|His)(\d+)$", substrate)
            if m:
                substrate = m.group(1)
                res_map = {"Ser": "S", "Thr": "T", "Tyr": "Y", "His": "H"}
                site = res_map.get(m.group(2), m.group(2)[0].upper()) + m.group(3)

        # Only include phosphorylation entries
        mech = rec.get("mechanism", rec.get("MECHANISM", "")).lower()
        if mech and "phosphorylation" not in mech:
            continue

        if kinase and substrate and site:
            entries.append({
                "kinase_gene": kinase, "substrate_gene": substrate,
                "phospho_site": site, "substrate_uniprot": _pick(rec, _up),
                "heptameric_peptide": _pick(rec, _pep),
            })
    return entries


def tool_fetch_and_parse_db(url: str, database_name: str, accumulator: EntryAccumulator) -> dict:
    """Download phospho data from a URL, auto-parse, and accumulate entries."""
    try:
        # Handle paginated REST APIs (UniProt-style)
        if "uniprotkb" in url and "search" in url:
            return _fetch_uniprot_paginated(url, database_name, accumulator)

        resp = _http_get(url, timeout=180)
        raw = resp.read()
        ct = (resp.headers.get("Content-Type") or "").lower()

        entries = []
        if "gzip" in ct or "x-gzip" in ct or url.endswith(".gz"):
            entries = _parse_gzipped_tsv(raw, database_name)
        elif "json" in ct:
            # Try UniProt protein records first, then generic JSON
            entries = _parse_uniprot_json(raw)
            if not entries:
                entries = _parse_generic_json(raw, database_name)
        else:
            text = raw.decode("utf-8", errors="replace")
            if "\t" in text[:2000]:
                entries = _parse_plain_tsv(text, database_name)

        before = accumulator.size()
        for e in entries:
            accumulator.add(
                e.get("kinase_gene", ""), e.get("substrate_gene", ""),
                e.get("phospho_site", ""), e.get("substrate_uniprot", ""),
                e.get("heptameric_peptide", ""), database_name,
            )
        new_unique = accumulator.size() - before

        sample = entries[:3] if entries else []
        return {
            "status": "success",
            "database": database_name,
            "url": url,
            "entries_parsed": len(entries),
            "new_unique_entries": new_unique,
            "total_accumulated": accumulator.size(),
            "sample_entries": sample,
        }
    except Exception as e:
        return {"status": "error", "database": database_name, "url": url, "error": str(e)}


def _fetch_uniprot_paginated(base_url, db_name, accumulator):
    """Paginate through UniProt REST API results."""
    # Ensure query params are present
    if "query=" not in base_url:
        query = ("(organism_id:9606) AND (reviewed:true) AND "
                 "(ft_mod_res:Phosphoserine OR ft_mod_res:Phosphothreonine "
                 "OR ft_mod_res:Phosphotyrosine)")
        base_url = f"{base_url}?query={urllib.parse.quote(query)}&format=json&size=500&fields=accession,gene_names,ft_mod_res"
    elif "size=" not in base_url:
        base_url += "&size=500"
    if "fields=" not in base_url:
        base_url += "&fields=accession,gene_names,ft_mod_res"
    if "format=" not in base_url:
        base_url += "&format=json"

    total_entries = 0
    total_proteins = 0
    cursor = None
    page = 0

    while True:
        url = base_url
        if cursor:
            url += f"&cursor={cursor}"
        try:
            resp = _http_get(url, timeout=120)
            link_hdr = resp.headers.get("Link", "")
            next_cursor = None
            if link_hdr:
                for part in link_hdr.split(","):
                    if 'rel="next"' in part:
                        m = re.search(r"cursor=([^&>\"]+)", part)
                        if m:
                            next_cursor = m.group(1)
            raw = resp.read()
            entries = _parse_uniprot_json(raw)
            data = json.loads(raw)
            n_proteins = len(data.get("results", []))
            total_proteins += n_proteins
            total_entries += len(entries)
            for e in entries:
                accumulator.add(
                    e.get("kinase_gene", ""), e.get("substrate_gene", ""),
                    e.get("phospho_site", ""), e.get("substrate_uniprot", ""),
                    e.get("heptameric_peptide", ""), db_name,
                )
            page += 1
            if not next_cursor or n_proteins == 0:
                break
            cursor = next_cursor
        except Exception as e:
            return {
                "status": "partial",
                "database": db_name,
                "proteins_processed": total_proteins,
                "entries_parsed": total_entries,
                "total_accumulated": accumulator.size(),
                "error_on_page": page,
                "error": str(e),
            }

    return {
        "status": "success",
        "database": db_name,
        "proteins_processed": total_proteins,
        "entries_parsed": total_entries,
        "total_accumulated": accumulator.size(),
        "pages_fetched": page,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL DEFINITIONS (Anthropic format)
# ═══════════════════════════════════════════════════════════════════════════════

def _convert_openai_tool(t):
    """Convert an OpenAI-format tool definition to Anthropic format."""
    fn = t["function"]
    return {
        "name": fn["name"],
        "description": fn["description"],
        "input_schema": fn["parameters"],
    }


def get_all_tool_definitions():
    """Return all tool definitions in Anthropic format."""
    # Database tools (from DatabaseTools)
    db_tools = [_convert_openai_tool(t) for t in DatabaseTools.get_tool_definitions()]

    # Web + custom tools
    extra_tools = [
        {
            "name": "web_search",
            "description": (
                "Search the web using DuckDuckGo. Returns a list of result URLs. "
                "Use this to find database websites, API documentation, and download links."
            ),
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
            "description": (
                "Fetch the content of a web page (truncated to ~15000 characters). "
                "Use this to read API documentation, download pages, or inspect data formats. "
                "For downloading large datasets, use fetch_and_parse_db instead."
            ),
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
            "description": (
                "Download phosphorylation data from a URL, automatically detect the format "
                "(gzipped TSV, plain TSV, JSON, paginated REST API), parse kinase-substrate-site "
                "entries, and accumulate them internally. Returns a summary with counts and "
                "sample entries. Supports: PSP-style gzipped TSV, SIGNOR-style headerless TSV, "
                "and UniProt REST API with paginated JSON. "
                "Call this for each database you discover. The parsed entries are stored "
                "internally and will be included when you call submit_atlas."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to download data from"},
                    "database_name": {
                        "type": "string",
                        "description": "Name of the database (for source attribution)",
                    },
                },
                "required": ["url", "database_name"],
            },
        },
        {
            "name": "submit_atlas",
            "description": (
                "Submit the completed phosphorylation atlas. All entries previously accumulated "
                "via fetch_and_parse_db calls are automatically included, deduplicated, and "
                "sorted. Provide a strategy_summary describing how you curated the data."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "strategy_summary": {
                        "type": "string",
                        "description": "Summary of the curation strategy used",
                    },
                },
                "required": ["strategy_summary"],
            },
        },
    ]

    return db_tools + extra_tools


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

class ClaudeSonnetAgent:
    """Autonomous Claude Sonnet agent for phosphorylation atlas curation."""

    def __init__(self, api_key, model="claude-sonnet-4-6", max_turns=50):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.max_turns = max_turns
        self.db_tools = DatabaseTools("databases")
        self.accumulator = EntryAccumulator()
        self.tool_count = 0
        self.strategy_summary = ""
        self.trace = []

    def _dispatch(self, tool_name, tool_input):
        """Route a tool call to the right implementation."""
        self.tool_count += 1

        # Database tools
        db_tool_names = {
            "list_databases", "get_stats", "list_kinases", "list_substrates",
            "query_by_kinase", "query_by_substrate", "query_by_site",
            "query_all_dbs", "search",
        }
        if tool_name in db_tool_names:
            return self.db_tools.dispatch(tool_name, tool_input)

        # Web tools
        if tool_name == "web_search":
            return tool_web_search(tool_input.get("query", ""))
        if tool_name == "web_fetch":
            return tool_web_fetch(tool_input.get("url", ""))
        if tool_name == "fetch_and_parse_db":
            return tool_fetch_and_parse_db(
                tool_input.get("url", ""),
                tool_input.get("database_name", "Unknown"),
                self.accumulator,
            )
        if tool_name == "submit_atlas":
            self.strategy_summary = tool_input.get("strategy_summary", "")
            entries = self.accumulator.finalize()
            return {
                "status": "accepted",
                "entries_received": len(entries),
                "message": f"Atlas submitted with {len(entries)} deduplicated entries.",
            }

        return {"error": f"Unknown tool: {tool_name}"}

    def run(self, system_prompt):
        """Run the autonomous agent loop."""
        messages = [{"role": "user", "content": "Begin."}]
        tool_defs = get_all_tool_definitions()
        submitted = False

        print(f"[AGENT] Starting Claude Sonnet agent (model={self.model})")
        print(f"[AGENT] Tools: {[t['name'] for t in tool_defs]}")

        for turn in range(self.max_turns):
            # Call the model with retry logic
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
                    print(f"[AGENT] Rate limited, waiting {wait}s (attempt {attempt + 1}/5)...")
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

            # Collect text and tool_use blocks
            text_parts = []
            tool_uses = []
            for block in response.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)
                if block.type == "tool_use":
                    tool_uses.append(block)

            if text_parts:
                combined_text = " ".join(text_parts)
                preview = combined_text[:200]
                print(f"[AGENT] Turn {turn + 1}: {preview}{'...' if len(combined_text) > 200 else ''}")

            if not tool_uses:
                # No tool calls — agent is done talking
                print(f"[AGENT] Agent finished (no tool calls). stop_reason={response.stop_reason}")
                break

            # Add assistant message to conversation
            messages.append({"role": "assistant", "content": response.content})

            # Execute tool calls
            tool_results = []
            for tool_use in tool_uses:
                t0 = time.time()
                result = self._dispatch(tool_use.name, tool_use.input)
                elapsed = time.time() - t0

                # Truncate result for context (keep under 50k chars)
                result_str = json.dumps(result)
                if len(result_str) > 50000:
                    result_str = result_str[:50000] + '..."}'

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": result_str,
                })

                # Log
                input_preview = json.dumps(tool_use.input)[:100]
                result_preview = result_str[:150]
                print(f"[TOOL {self.tool_count}] {tool_use.name}({input_preview}) "
                      f"-> {result_preview}{'...' if len(result_str) > 150 else ''} "
                      f"({elapsed:.1f}s)")

                self.trace.append({
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
            print(f"[AGENT] Auto-submitting {len(entries)} accumulated entries (budget reached)")
            self.strategy_summary = (
                f"Autonomous agent ran {self.tool_count} tool calls across {turn + 1} turns. "
                f"Auto-submitted accumulated entries from discovered databases."
            )
        print(f"[AGENT] Done. {self.tool_count} tool calls, {len(entries)} entries")
        return entries


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: set ANTHROPIC_API_KEY environment variable")
        print("  export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    out_dir = Path("contributions/claude_sonnet_naive")
    out_dir.mkdir(parents=True, exist_ok=True)
    scores_dir = out_dir / "scores"
    scores_dir.mkdir(parents=True, exist_ok=True)

    # Load naive prompt
    prompt_path = Path("agents/prompts/naive.txt")
    system_prompt = prompt_path.read_text().strip()
    print(f"[SETUP] Prompt: {prompt_path} ({len(system_prompt)} chars)")
    print(f"[SETUP] Model: claude-sonnet-4-6")
    print(f"[SETUP] Condition: naive (zero-shot)")

    # Run agent
    t0 = time.time()
    agent = ClaudeSonnetAgent(api_key)
    entries = agent.run(system_prompt)
    elapsed = time.time() - t0

    print(f"\n[RESULT] Elapsed: {elapsed:.1f}s ({elapsed / 60:.1f}m)")
    print(f"[RESULT] Tool calls: {agent.tool_count}")
    print(f"[RESULT] Atlas: {len(entries)} entries")

    # Save atlas.json
    atlas_path = out_dir / "atlas.json"
    with open(atlas_path, "w") as f:
        json.dump(entries, f, indent=2)

    # Compute stats
    db_counts = {}
    multi_db = 0
    for e in entries:
        for db in e["supporting_databases"]:
            db_counts[db] = db_counts.get(db, 0) + 1
        if len(e["supporting_databases"]) >= 2:
            multi_db += 1

    # Save run_log.json
    run_log = {
        "agent": "Claude Sonnet 4.6",
        "prompt": "naive (zero-shot)",
        "strategy": agent.strategy_summary,
        "autonomous": True,
        "databases_accessed": sorted(db_counts.keys()),
        "tool_calls": agent.tool_count,
        "raw_counts": {k: v for k, v in db_counts.items()},
        "merged_atlas": len(entries),
        "unique_kinases": len(set(e["kinase_gene"] for e in entries)),
        "unique_substrates": len(set(e["substrate_gene"] for e in entries)),
        "multi_db_entries": multi_db,
        "elapsed_seconds": round(elapsed, 1),
        "trace": agent.trace,
    }
    with open(out_dir / "run_log.json", "w") as f:
        json.dump(run_log, f, indent=2)

    # Run scorer
    print("\n[SCORE] Running evaluation scorer...")
    gold_path = "gold_standard/parsed/phosphoatlas_gold.json"
    if Path(gold_path).exists():
        from evaluation.scorer import load_gold, score_atlas, score_per_kinase
        gold = load_gold(gold_path)
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
        print(f"[SCORE]   Atlas size:       {ov['atlas_size']}")
        print(f"[SCORE]   Recall:           {ov['recall']}")
        print(f"[SCORE]   Precision:        {ov['precision']}")
        print(f"[SCORE]   F1:               {ov['f1']}")
        print(f"[SCORE]   Kinases found:    {ov['kinases_found']}")
        print(f"[SCORE]   Multi-DB:         {ov['multi_db_pct']}%")
        print(f"[SCORE]   Peptide accuracy: {ov['peptide_accuracy']}")
        print(f"[SCORE]   {'PASS' if ov['f1'] >= 0.75 else 'WARN'}: F1 = {ov['f1']}")
        print(f"[SCORE]   {'PASS' if ov['recall'] >= 0.90 else 'WARN'}: Recall = {ov['recall']}")
    else:
        print(f"[SCORE]   Gold standard not found at {gold_path}")

    print(f"\n{'=' * 60}")
    print(f"Outputs in: {out_dir}/")


if __name__ == "__main__":
    main()
