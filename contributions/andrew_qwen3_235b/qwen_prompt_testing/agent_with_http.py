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
import uuid
from pathlib import Path

import requests as _requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── Prompt ────────────────────────────────────────────────────────────────────

EXPLICIT_PROMPT = """
You are a bioinformatics researcher tasked with building a comprehensive
human protein phosphorylation atlas.

Your goal: curate ALL known human kinase-substrate-phosphosite relationships
by querying the following two public databases via http_get. Do NOT use
list_databases or get_stats — those local tools are empty. Use http_get only.

=== STEP 1: Pull all SIGNOR data (one call) ===

  SIGNOR REST API — https://signor.uniroma2.it/API/getHumanData.php
    No parameters needed. Returns full human dataset as TSV.
    Filter rows: MECHANISM == "phosphorylation" AND TYPEA == "protein"
    ENTITYA = kinase gene, ENTITYB = substrate gene
    RESIDUE = phospho site, SEQUENCE = heptameric peptide, IDB = UniProt

  Make this call first. The system will parse and accumulate the entries
  automatically. Note every unique ENTITYA (kinase gene name) you see in
  the results — you will use that list in Step 2.

=== STEP 2: Query UniProt for each kinase from Step 1 ===

  UniProt REST API — https://rest.uniprot.org/uniprotkb/search
    For EACH kinase gene name you collected from SIGNOR, make one query:
      ?query=organism_id:9606+AND+reviewed:true+AND+ft_mod_res:KINASE_NAME
      &fields=gene_names,ft_mod_res,accession&format=json&size=500

    IMPORTANT:
    - ft_mod_res takes the KINASE GENE NAME (e.g. CDK1, EGFR, MAPK1).
      Do NOT use "phospho", "phosphoserine", or other modification keywords.
    - Do NOT enumerate all human gene names first. Use the kinase list from
      SIGNOR directly as your query list.
    - If a page has a Link header with rel="next", follow it for pagination.
    - After exhausting the SIGNOR kinase list, also query common kinases not
      in SIGNOR: AKT1, AKT2, AKT3, MTOR, PIK3CA, PTEN, TP53, BRCA1, ATM,
      ATR, CHEK1, CHEK2, MDM2, RB1, CDKN1A, CDKN2A, KRAS, BRAF, RAF1,
      MAP2K1, MAP2K2, MAPK1, MAPK3, MAPK8, MAPK14, GSK3A, GSK3B, PRKACA,
      PRKACB, PRKCА, PRKCB, PRKCD, PRKCE, PRKCI, PRKCZ, AURKA, AURKB,
      PLK1, PLK2, PLK3, NEK1, NEK2, NEK6, NEK7, DYRK1A, DYRK1B, CLK1,
      CSNK1A1, CSNK1D, CSNK1E, CSNK2A1, CSNK2A2, VRK1, VRK2, TTK, BUB1,
      HASPIN, RPS6KB1, RPS6KB2, RPS6KA1, RPS6KA2, RPS6KA3, EIF2AK1,
      EIF2AK2, EIF2AK3, EIF2AK4, AMPK, STK11, MARK1, MARK2, MARK3.

=== OUTPUT FORMAT ===

For each relationship capture:
  - kinase_gene          : kinase gene symbol (e.g. CDK1)
  - substrate_gene       : substrate gene symbol (e.g. RB1)
  - phospho_site         : residue + position (e.g. S807, T308, Y15)
  - heptameric_peptide   : 7 amino acids around the site (if available)
  - substrate_uniprot    : UniProt accession (if available)
  - supporting_databases : list of sources — ["UniProt"], ["SIGNOR"], or both

Requirements:
  1. Be EXHAUSTIVE — missing entries is worse than extra entries.
  2. Cross-reference — if a relationship appears in both databases, list both.
  3. Do NOT fabricate. Only include what the APIs return.
  4. Do NOT stop after SIGNOR alone — UniProt adds significant coverage.

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

    # ── Text-mode tool call recovery ──────────────────────────────────────────

    def _parse_tool_calls(self, response) -> list[tuple[str, dict]]:
        """Extend strict parsing with <tool_call> XML fallback.

        Qwen3 on Together AI occasionally emits tool calls as raw
        <tool_call>{"name":...,"arguments":...}</tool_call> text instead of
        structured function calls. The base class intentionally rejects these
        (Paper 1 compliance measure). We recover them here for the HTTP
        experiment only, so the loop keeps running after SIGNOR.
        """
        calls = super()._parse_tool_calls(response)
        if calls:
            return calls

        text = response.choices[0].message.content or ""
        # Match everything between <tool_call> tags — avoids non-greedy {.*?}
        # breaking on nested JSON braces
        matches = re.findall(r'<tool_call>([\s\S]*?)</tool_call>', text)
        if not matches:
            return []

        self._pending_tool_call_ids = []
        parsed = []
        fake_tool_calls = []

        for raw in matches:
            try:
                obj = json.loads(raw.strip())
            except json.JSONDecodeError:
                continue
            name = obj.get("name", "")
            args = obj.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if not name:
                continue
            tc_id = f"call_{uuid.uuid4().hex[:8]}"
            parsed.append((name, args))
            self._pending_tool_call_ids.append(tc_id)
            fake_tool_calls.append({
                "id": tc_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            })

        if fake_tool_calls and self.messages and self.messages[-1]["role"] == "assistant":
            self._log(f"[TEXT-MODE] Recovered {len(fake_tool_calls)} tool call(s) from text content")
            self.messages[-1]["tool_calls"] = fake_tool_calls

        return parsed

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

        import time as _time
        try:
            # SIGNOR sometimes returns truncated responses under load — retry
            max_attempts = 3 if "signor" in url.lower() else 1
            resp = None
            for attempt in range(max_attempts):
                resp = _requests.get(url, params=params, timeout=60)
                resp.raise_for_status()
                if "signor" in url.lower():
                    row_count = len(resp.text.strip().splitlines())
                    if row_count < 100 and attempt < max_attempts - 1:
                        self._log(f"[SIGNOR] Only {row_count} rows — retrying ({attempt + 2}/{max_attempts})")
                        _time.sleep(3)
                        continue
                break

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
