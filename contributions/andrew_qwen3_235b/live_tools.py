"""
Live UniProt database tool layer for the PhosphoAtlas benchmark.

This is a drop-in replacement for databases.tools.DatabaseTools that queries
the UniProt REST API in real time instead of reading local database files.

Why this exists
---------------
The benchmark's local database files (PSP, SIGNOR, UniProt snapshots) are not
committed to the repo. Rather than block on obtaining them, this module queries
UniProt's public REST API live. The benchmark is explicitly designed to tolerate
database version differences between agents — precision/recall are the primary
metrics, not a fixed-universe comparison.

Coverage limitation (important for methods)
-------------------------------------------
UniProt annotates phosphosites with kinase attribution only when the experimental
evidence specifically names the kinase (e.g. "Phosphoserine; by CDK5"). Many
verified phosphosites in PhosphoSitePlus and SIGNOR lack this annotation in
UniProt. This implementation therefore has lower recall than a full PSP+SIGNOR
+UniProt stack, but all returned entries are well-supported. This tradeoff
should be stated in the Paper 1 methods section.

Interface contract
------------------
Matches databases.tools.DatabaseTools:
  - dispatch(tool_name, arguments) -> dict
  - call_count: int property
  - call_log: list property
  - reset_log()
  - get_tool_definitions() classmethod (delegates to DatabaseTools)

Data source
-----------
UniProt REST API: https://rest.uniprot.org
Only queries reviewed (Swiss-Prot) human entries.
"""

import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from databases.tools import DatabaseTools

UNIPROT_BASE = "https://rest.uniprot.org/uniprotkb"
HUMAN_TAXON = "9606"
REQUEST_DELAY = 0.1   # seconds between requests — respect UniProt rate limits

# Maps full residue name in UniProt description to single-letter code
RESIDUE_MAP = {
    "phosphoserine":    "S",
    "phosphothreonine": "T",
    "phosphotyrosine":  "Y",
    "phosphohistidine": "H",
    "phospholysine":    "K",
    "phosphocysteine":  "C",
    "phosphoaspartate": "D",
}


def _parse_kinases_from_description(description: str) -> list[str]:
    """Extract kinase gene symbols from a UniProt PTM description.

    UniProt format examples:
      "Phosphoserine; by CDK5"
      "Phosphoserine; by CDK1 and CDK5"
      "Phosphoserine; by PKA and PKC"
      "Phosphoserine"  (no kinase — returns empty list)
    """
    m = re.search(r";\s*by\s+(.+)$", description, re.IGNORECASE)
    if not m:
        return []
    raw = m.group(1)
    # Split on " and ", ",", "/"
    parts = re.split(r"\s+and\s+|,\s*|/", raw)
    return [p.strip() for p in parts if p.strip()]


def _phospho_site_code(description: str, position: int) -> Optional[str]:
    """Convert a UniProt PTM description + position to phospho-site notation.

    E.g. ("Phosphoserine; by CDK5", 807) -> "S807"
    """
    lower = description.lower()
    for name, code in RESIDUE_MAP.items():
        if lower.startswith(name):
            return f"{code}{position}"
    return None


def _fetch_uniprot(params: dict, max_pages: int = 20) -> list[dict]:
    """Paginate through UniProt search results and return all entries."""
    results = []
    url = f"{UNIPROT_BASE}/search"
    params = {**params, "format": "json", "size": 500}

    pages = 0
    while url and pages < max_pages:
        resp = requests.get(url, params=params if pages == 0 else None, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("results", []))
        # Follow Link header for next page
        link = resp.headers.get("Link", "")
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = m.group(1) if m else None
        params = None  # only pass params on first request
        pages += 1
        time.sleep(REQUEST_DELAY)

    return results


def _entries_from_uniprot_record(record: dict) -> list[dict]:
    """Extract phosphosite entries from a single UniProt record.

    Returns a list of dicts, one per attributed phosphosite.
    Only includes sites that have explicit kinase attribution.
    """
    gene = ""
    genes = record.get("genes", [])
    if genes:
        primary = genes[0].get("geneName", {})
        gene = primary.get("value", "")

    accession = record.get("primaryAccession", "")

    entries = []
    for feature in record.get("features", []):
        if feature.get("type") != "Modified residue":
            continue
        desc = feature.get("description", "")
        if "phospho" not in desc.lower():
            continue

        position = feature.get("location", {}).get("start", {}).get("value")
        if not position:
            continue

        site = _phospho_site_code(desc, position)
        if not site:
            continue

        kinases = _parse_kinases_from_description(desc)
        if not kinases:
            continue  # skip unannotated sites

        for kinase in kinases:
            entries.append({
                "kinase_gene": kinase,
                "substrate_gene": gene,
                "substrate_uniprot": accession,
                "phospho_site": site,
                "heptameric_peptide": "",   # not fetched by default (needs sequence)
                "supporting_databases": ["UniProt"],
            })

    return entries


class LiveDatabaseTools:
    """UniProt live API backend with the same interface as DatabaseTools."""

    DB_ID = "uniprot"

    def __init__(self):
        self._call_log: list[dict] = []
        self._kinase_cache: dict[str, list[dict]] = {}   # gene -> entries
        self._substrate_cache: dict[str, list[dict]] = {} # gene -> entries
        self._kinase_list_cache: Optional[list[str]] = None

    # -------------------------------------------------------------------------
    # Interface contract
    # -------------------------------------------------------------------------

    @property
    def call_count(self) -> int:
        return len(self._call_log)

    @property
    def call_log(self) -> list:
        return list(self._call_log)

    def reset_log(self):
        self._call_log.clear()

    @staticmethod
    def get_tool_definitions() -> list:
        return DatabaseTools.get_tool_definitions()

    def dispatch(self, tool_name: str, arguments: dict) -> dict:
        self._call_log.append({"tool": tool_name, **arguments})
        db = arguments.get("db", self.DB_ID)

        try:
            if tool_name == "list_databases":
                return self._list_databases()
            elif tool_name == "list_kinases":
                return self._list_kinases(db, arguments.get("offset", 0), arguments.get("limit", 100))
            elif tool_name == "list_substrates":
                return self._list_substrates(db, arguments.get("offset", 0), arguments.get("limit", 100))
            elif tool_name == "get_stats":
                return self._get_stats(db)
            elif tool_name == "search":
                return self._search(db, arguments.get("keyword", ""), arguments.get("limit", 50))
            elif tool_name == "query_by_kinase":
                return self._query_by_kinase(db, arguments.get("gene", ""))
            elif tool_name == "query_by_substrate":
                return self._query_by_substrate(db, arguments.get("gene", ""))
            elif tool_name == "query_by_site":
                return self._query_by_site(db, arguments.get("gene", ""), arguments.get("site", ""))
            elif tool_name == "query_all_dbs":
                return self._query_all_dbs(arguments.get("gene", ""))
            else:
                return {"error": f"Unknown tool: {tool_name}"}
        except requests.RequestException as e:
            return {"error": f"UniProt API request failed: {e}"}

    # -------------------------------------------------------------------------
    # Tool implementations
    # -------------------------------------------------------------------------

    def _list_databases(self) -> dict:
        return {
            "databases": [
                {
                    "id": "uniprot",
                    "name": "UniProt/UniProtKB (live)",
                    "description": (
                        "Reviewed (Swiss-Prot) human protein entries with phosphorylation "
                        "site annotations attributed to specific kinases. Queried live from "
                        "the UniProt REST API. Note: only sites with explicit kinase attribution "
                        "are returned — unattributed phosphosites are excluded."
                    ),
                    "query_tools": [
                        "list_kinases", "list_substrates", "query_by_kinase",
                        "query_by_substrate", "query_by_site", "search", "get_stats"
                    ],
                }
            ]
        }

    def _query_by_kinase(self, db: str, gene: str) -> dict:
        """Fetch all substrates phosphorylated by the given kinase gene."""
        if not gene:
            return {"error": "gene is required"}

        if gene in self._kinase_cache:
            entries = self._kinase_cache[gene]
        else:
            records = _fetch_uniprot({
                "query": (
                    f'organism_id:{HUMAN_TAXON} AND reviewed:true '
                    f'AND ft_mod_res:{gene}'
                ),
                "fields": "gene_names,ft_mod_res,accession",
            })
            entries = []
            for record in records:
                entries.extend(_entries_from_uniprot_record(record))
            # Keep only entries attributed to this kinase
            entries = [e for e in entries if e["kinase_gene"].upper() == gene.upper()]
            self._kinase_cache[gene] = entries

        return {"kinase": gene, "entries": entries, "count": len(entries)}

    def _query_by_substrate(self, db: str, gene: str) -> dict:
        """Fetch all kinase-attributed phosphosites on a substrate gene."""
        if not gene:
            return {"error": "gene is required"}

        if gene in self._substrate_cache:
            entries = self._substrate_cache[gene]
        else:
            records = _fetch_uniprot({
                "query": (
                    f'organism_id:{HUMAN_TAXON} AND reviewed:true '
                    f'AND gene_exact:{gene} AND ft_mod_res:Phospho'
                ),
                "fields": "gene_names,ft_mod_res,accession",
            })
            entries = []
            for record in records:
                entries.extend(_entries_from_uniprot_record(record))
            self._substrate_cache[gene] = entries

        return {"substrate": gene, "entries": entries, "count": len(entries)}

    def _query_by_site(self, db: str, gene: str, site: str) -> dict:
        """Look up a specific kinase-substrate-site entry."""
        result = self._query_by_substrate(db, gene)
        entries = [e for e in result.get("entries", []) if e["phospho_site"] == site]
        return {"gene": gene, "site": site, "entries": entries, "count": len(entries)}

    def _list_kinases(self, db: str, offset: int, limit: int) -> dict:
        """Return human kinase gene symbols using UniProt's Kinase keyword (KW-0418).

        This queries UniProt directly for reviewed human proteins annotated as
        kinases — giving a comprehensive human kinome list (~500+ entries) that
        the agent can iterate through with query_by_kinase.
        """
        if self._kinase_list_cache is None:
            records = _fetch_uniprot({
                "query": (
                    f'organism_id:{HUMAN_TAXON} AND reviewed:true '
                    f'AND keyword:KW-0418'   # UniProt keyword: Kinase
                ),
                "fields": "gene_names",
            }, max_pages=20)  # ~500 kinases at 500/page needs ~1-2 pages

            kinases: list[str] = []
            for record in records:
                genes = record.get("genes", [])
                if genes:
                    gene = genes[0].get("geneName", {}).get("value", "")
                    if gene:
                        kinases.append(gene)

            self._kinase_list_cache = sorted(set(kinases))

        page = self._kinase_list_cache[offset: offset + limit]
        return {
            "kinases": page,
            "total": len(self._kinase_list_cache),
            "offset": offset,
            "limit": limit,
        }

    def _list_substrates(self, db: str, offset: int, limit: int) -> dict:
        """Return human proteins with at least one attributed phosphosite."""
        records = _fetch_uniprot({
            "query": (
                f'organism_id:{HUMAN_TAXON} AND reviewed:true '
                f'AND ft_mod_res:Phospho'
            ),
            "fields": "gene_names,accession",
        }, max_pages=1)

        substrates = []
        for record in records:
            genes = record.get("genes", [])
            if genes:
                gene = genes[0].get("geneName", {}).get("value", "")
                if gene:
                    substrates.append(gene)

        page = substrates[offset: offset + limit]
        return {
            "substrates": page,
            "count": len(page),
            "offset": offset,
            "limit": limit,
        }

    def _get_stats(self, db: str) -> dict:
        return {
            "database": "UniProt/UniProtKB (live)",
            "note": (
                "Live queries — counts are approximate. "
                "Use query_by_kinase for precise per-kinase data."
            ),
        }

    def _search(self, db: str, keyword: str, limit: int) -> dict:
        """Keyword search across human phosphoprotein annotations."""
        records = _fetch_uniprot({
            "query": (
                f'organism_id:{HUMAN_TAXON} AND reviewed:true '
                f'AND ft_mod_res:Phospho AND ({keyword})'
            ),
            "fields": "gene_names,ft_mod_res,accession",
        }, max_pages=1)

        entries = []
        for record in records[:limit]:
            entries.extend(_entries_from_uniprot_record(record))

        return {"keyword": keyword, "entries": entries[:limit], "count": len(entries[:limit])}

    def _query_all_dbs(self, gene: str) -> dict:
        """Query UniProt for a gene both as kinase and as substrate."""
        as_kinase = self._query_by_kinase(self.DB_ID, gene)
        as_substrate = self._query_by_substrate(self.DB_ID, gene)
        return {
            "gene": gene,
            "as_kinase": as_kinase,
            "as_substrate": as_substrate,
        }
