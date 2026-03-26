"""
QwenAgentWithHTTP — extends QwenAgent with a generic http_get tool.

This agent is used for the URL-injection experiment (Paper 1 / tentative).
The model is given UniProt and SIGNOR API endpoints in the prompt and must
figure out how to query them, paginate, and map responses to the atlas schema.

Differences from base QwenAgent:
  - Adds http_get to tool_definitions so the model can make real HTTP calls
  - Tracks cumulative token cost and logs a warning at $50 (per Hui's guidance)
  - Lives in qwen_prompt_testing/ — does not affect the naive baseline run

Output: qwen_prompt_testing/explicit_prompt_test_atlas.json
"""

from contributions.andrew_qwen3_235b.agent_runner import QwenAgent
import json
import re
import sys
from pathlib import Path

import requests as _requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── Prompt ────────────────────────────────────────────────────────────────────

EXPLICIT_PROMPT = """
You are a bioinformatics researcher tasked with building a comprehensive
human protein phosphorylation atlas.

Your goal: curate ALL known human kinase-substrate-phosphosite relationships
by querying the following public databases via http_get. Do NOT use
list_databases or get_stats — those local tools are empty. Use http_get only.

  UniProt REST API  — https://rest.uniprot.org/uniprotkb/search
    Query Swiss-Prot human proteins. The ft_mod_res field takes a KINASE NAME
    (e.g. CDK1, EGFR, MAPK1) — NOT a modification keyword like "phospho".
    Example: ?query=organism_id:9606+AND+reviewed:true+AND+ft_mod_res:CDK1
             &fields=gene_names,ft_mod_res,accession&format=json&size=500
    Iterate over many kinase names to build exhaustive coverage.
    Use the Link header cursor for pagination when results exceed page size.

  SIGNOR REST API   — https://signor.uniroma2.it/API/getHumanData.php
    Returns all curated human signaling relationships as TSV (no parameters needed).
    Columns: ENTITYA, TYPEA, IDA, ..., MECHANISM, RESIDUE, SEQUENCE, ...
    Filter rows where MECHANISM == "phosphorylation" and TYPEA == "protein".
    ENTITYA = kinase gene, ENTITYB = substrate gene, RESIDUE = phospho site,
    SEQUENCE = heptameric peptide, IDB = substrate UniProt accession.

For each relationship, capture:
  - kinase_gene          : kinase gene symbol (e.g. CDK1)
  - substrate_gene       : substrate gene symbol (e.g. RB1)
  - phospho_site         : residue + position (e.g. S807, T308, Y15)
  - heptameric_peptide   : 7 amino acids around the site (if available)
  - substrate_uniprot    : UniProt accession (if available)
  - supporting_databases : list of sources (e.g. ["UniProt", "SIGNOR"])

Requirements:
  1. Be EXHAUSTIVE — missing entries is worse than having extra entries.
  2. Cross-reference — if a relationship appears in both databases, list both.
  3. Do NOT fabricate data. Only include what the APIs return.

When finished, call submit_atlas with your complete results.
""".strip()

# ── HTTP tool definition ──────────────────────────────────────────────────────

HTTP_GET_TOOL = {
    "type": "function",
    "function": {
        "name": "http_get",
        "description": (
            "Make an HTTP GET request to a URL. Use this to query external "
            "APIs such as UniProt or SIGNOR. Large responses are truncated "
            "to 8000 characters — use pagination parameters if available."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full URL to request",
                },
                "params": {
                    "type": "object",
                    "description": "Optional query parameters as key-value pairs",
                },
            },
            "required": ["url"],
        },
    },
}

# ── Agent ─────────────────────────────────────────────────────────────────────

# Together AI Qwen3-235B pricing (approximate, per Hui: checkpoint at $50)
_COST_PER_1M_INPUT = 0.90
_COST_PER_1M_OUTPUT = 0.90
_COST_WARN_USD = 50.0


class QwenAgentWithHTTP(QwenAgent):
    """QwenAgent extended with http_get tool and token cost tracking."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # Append http_get to the tool list the model receives
        self.tool_definitions.append(HTTP_GET_TOOL)

        # Cumulative token cost tracker
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._cost_warned = False

    # ── Cost tracking ─────────────────────────────────────────────────────────

    def _track_cost(self, response):
        """Update token counts from response usage and warn at $50."""
        usage = getattr(response, "usage", None)
        if not usage:
            return

        self._total_input_tokens += getattr(usage, "prompt_tokens", 0)
        self._total_output_tokens += getattr(usage, "completion_tokens", 0)

        cost = (
            self._total_input_tokens / 1_000_000 * _COST_PER_1M_INPUT +
            self._total_output_tokens / 1_000_000 * _COST_PER_1M_OUTPUT
        )

        self._log(f"[COST] ~${cost:.2f} so far "
                  f"({self._total_input_tokens} in / {self._total_output_tokens} out tokens)")

        if cost >= _COST_WARN_USD and not self._cost_warned:
            self._cost_warned = True
            self._log(f"[COST WARNING] Approaching $50 — pausing for investigation. "
                      f"Current cost: ${cost:.2f}")

    # ── Override _call_model to track cost ────────────────────────────────────

    def _call_model(self, messages, tools):
        response = super()._call_model(messages, tools)
        self._track_cost(response)
        return response

    # ── Entry parsers ──────────────────────────────────────────────────────────

    _RESIDUE_MAP = {
        "phosphoserine":    "S",
        "phosphothreonine": "T",
        "phosphotyrosine":  "Y",
        "phosphohistidine": "H",
    }

    def _parse_uniprot_entries(self, data: dict, kinase: str) -> list:
        """Extract atlas entries from a UniProt ft_mod_res:KINASE JSON response."""
        entries = []
        for result in data.get("results", []):
            accession = result.get("primaryAccession", "")
            genes = result.get("genes", [])
            substrate = genes[0].get("geneName", {}).get(
                "value", "") if genes else ""
            if not substrate:
                continue

            for feat in result.get("features", []):
                if feat.get("type") != "Modified residue":
                    continue
                desc = feat.get("description", "").lower()
                if kinase.lower() not in desc:
                    continue

                residue_type = next(
                    (v for k, v in self._RESIDUE_MAP.items() if k in desc), None
                )
                if not residue_type:
                    continue

                pos = feat.get("location", {}).get(
                    "start", {}).get("value", "")
                if not pos:
                    continue

                entries.append({
                    "kinase_gene":          kinase.upper(),
                    "substrate_gene":       substrate,
                    "phospho_site":         f"{residue_type}{pos}",
                    "heptameric_peptide":   "",
                    "substrate_uniprot":    accession,
                    "supporting_databases": ["UniProt"],
                })
        return entries

    def _parse_signor_entries(self, text: str) -> list:
        """Extract atlas entries from SIGNOR TSV response."""
        entries = []
        lines = text.strip().splitlines()
        if len(lines) < 2:
            return entries

        header = lines[0].split("\t")
        try:
            col = {name: i for i, name in enumerate(header)}
            ia, ta = col["ENTITYA"], col["TYPEA"]
            ib = col["ENTITYB"]
            mech = col["MECHANISM"]
            res = col["RESIDUE"]
            seq = col["SEQUENCE"]
            idb = col["IDB"]
        except KeyError:
            return entries

        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) <= max(ia, ta, ib, mech, res, seq, idb):
                continue
            if parts[mech] != "phosphorylation" or parts[ta] != "protein":
                continue
            kinase = parts[ia]
            substrate = parts[ib]
            site = parts[res]
            if not (kinase and substrate and site):
                continue
            entries.append({
                "kinase_gene":          kinase,
                "substrate_gene":       substrate,
                "phospho_site":         site,
                "heptameric_peptide":   parts[seq] if seq < len(parts) else "",
                "substrate_uniprot":    parts[idb] if idb < len(parts) else "",
                "supporting_databases": ["SIGNOR"],
            })
        return entries

    def _accumulate_http_entries(self, entries: list) -> int:
        """Deduplicate and add entries to the accumulator. Returns count added."""
        added = 0
        for entry in entries:
            key = (
                entry.get("kinase_gene", ""),
                entry.get("substrate_gene", ""),
                entry.get("phospho_site", ""),
            )
            if all(key) and key not in self._seen_triplets:
                self._seen_triplets.add(key)
                self._accumulated_entries.append(entry)
                added += 1
        return added

    # ── http_get dispatch ─────────────────────────────────────────────────────

    def _dispatch_http_get(self, arguments: dict) -> dict:
        """Execute an HTTP GET, accumulate parsed entries, return short body to model."""
        url = arguments.get("url", "")
        params = arguments.get("params", None)

        if not url:
            return {"error": "url is required"}

        try:
            resp = _requests.get(url, params=params, timeout=30)
            resp.raise_for_status()

            # ── SIGNOR: parse full TSV, return summary only ────────────────
            if "signor" in url.lower():
                entries = self._parse_signor_entries(resp.text)
                added = self._accumulate_http_entries(entries)
                self._log(f"[ACCUM] SIGNOR: {added} new entries accumulated "
                          f"({len(self._accumulated_entries)} total)")
                return {
                    "status":  resp.status_code,
                    "body":    (
                        f"[SIGNOR] {len(resp.text.splitlines())} rows fetched. "
                        f"{len(entries)} phosphorylation entries parsed, "
                        f"{added} new entries accumulated (deduped). "
                        f"Columns: ENTITYA=kinase, ENTITYB=substrate, "
                        f"MECHANISM, RESIDUE, SEQUENCE, IDB=UniProt."
                    ),
                }

            # ── UniProt ft_mod_res:KINASE: parse JSON, return truncated body
            if "uniprot" in url.lower():
                try:
                    data = resp.json()
                    query = (params or {}).get("query", "")
                    match = re.search(r'ft_mod_res:(\w+)', query)
                    if match:
                        kinase = match.group(1)
                        entries = self._parse_uniprot_entries(data, kinase)
                        added = self._accumulate_http_entries(entries)
                        self._log(f"[ACCUM] UniProt ft_mod_res:{kinase}: "
                                  f"{added} new entries accumulated "
                                  f"({len(self._accumulated_entries)} total)")
                    text = json.dumps(data)
                    if len(text) > 2000:
                        n = len(data.get("results", []))
                        text = text[:2000] + \
                            f"... [TRUNCATED — {n} results on this page]"
                    return {"status": resp.status_code, "body": text}
                except Exception:
                    text = resp.text[:2000]
                    return {"status": resp.status_code, "body": text}

            # ── Default: truncate to 2000 chars ───────────────────────────
            try:
                text = json.dumps(resp.json())
            except Exception:
                text = resp.text
            if len(text) > 2000:
                text = text[:2000] + "... [TRUNCATED — use pagination]"
            return {"status": resp.status_code, "body": text}

        except _requests.RequestException as e:
            return {"error": str(e)}

    # ── Override run() to intercept http_get before tools.dispatch ────────────

    def run(self, system_prompt: str, condition: str = "naive") -> dict:
        """Run loop with http_get interception and cost tracking reset."""
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._cost_warned = False

        # Patch tools.dispatch to handle http_get transparently
        original_dispatch = self.tools.dispatch

        def patched_dispatch(tool_name, arguments):
            if tool_name == "http_get":
                self._log(f"[HTTP] GET {arguments.get('url', '')[:100]}")
                return self._dispatch_http_get(arguments)
            return original_dispatch(tool_name, arguments)

        self.tools.dispatch = patched_dispatch
        result = super().run(system_prompt, condition)
        self.tools.dispatch = original_dispatch  # restore

        return result
