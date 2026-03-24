#!/usr/bin/env python3
"""
Universal Database Query Interface for the PhosphoAtlas Benchmark.

Provides a tool-use interface that ANY agent (Claude, GPT, open-source) can call.
The agent does NOT get direct file access — it must discover and curate
phosphorylation data through these query tools, just like a researcher would.

Available tools:
  DISCOVERY (what's in the database?):
    list_databases()              -> available databases and descriptions
    list_kinases(db)              -> all kinase gene symbols in a database
    list_substrates(db)           -> all substrate gene symbols in a database
    search(db, keyword)           -> search entries by keyword (gene, site, etc.)
    get_stats(db)                 -> summary statistics for a database

  RETRIEVAL (get specific data):
    query_by_kinase(db, gene)     -> all substrates/sites for a kinase
    query_by_substrate(db, gene)  -> all kinases that phosphorylate this substrate
    query_by_site(db, gene, site) -> specific kinase-substrate-site records

  CROSS-REFERENCE:
    query_all_dbs(gene)           -> query all databases for a gene at once

Usage as Python module:
    from databases.tools import DatabaseTools
    tools = DatabaseTools("databases/")
    tools.list_databases()

Usage as REST API (for remote agents):
    python -m databases.tools --serve --port 8000
"""
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional


class DatabaseTools:
    """Universal query interface over local phosphorylation databases."""

    def __init__(self, databases_dir: str):
        self.db_dir = Path(databases_dir)
        self._psp = None
        self._signor = None
        self._uniprot = None
        self._call_log = []  # track all queries for benchmarking

    def _log_call(self, tool: str, params: dict):
        self._call_log.append({"tool": tool, **params})

    @property
    def call_count(self) -> int:
        return len(self._call_log)

    @property
    def call_log(self) -> list:
        return list(self._call_log)

    def reset_log(self):
        self._call_log.clear()

    # === Lazy loaders ===

    def _load_psp(self):
        if self._psp is not None:
            return
        path = self.db_dir / "psp" / "Kinase_Substrate_Dataset"
        self._psp = {"by_kinase": defaultdict(list), "by_substrate": defaultdict(list),
                      "all_entries": [], "kinases": set(), "substrates": set()}
        if not path.exists():
            return
        with open(path, "r", errors="replace") as f:
            for _ in range(3):
                next(f, None)
            for row in csv.DictReader(f, delimiter="\t"):
                if row.get("SUB_ORGANISM", "").strip() != "human":
                    continue
                entry = {
                    "kinase_gene": row.get("GENE", "").strip(),
                    "substrate_gene": row.get("SUB_GENE", "").strip(),
                    "substrate_uniprot": row.get("SUB_ACC_ID", "").strip(),
                    "phospho_site": row.get("SUB_MOD_RSD", "").strip(),
                    "heptameric_peptide": row.get("SITE_+/-7_AA", "").strip(),
                    "site_group_id": row.get("SITE_GRP_ID", "").strip(),
                    "in_vivo": row.get("IN_VIVO_RXN", "").strip(),
                    "in_vitro": row.get("IN_VITRO_RXN", "").strip(),
                    "source": "PhosphoSitePlus",
                }
                k = entry["kinase_gene"]
                s = entry["substrate_gene"]
                if k and s:
                    self._psp["by_kinase"][k].append(entry)
                    self._psp["by_substrate"][s].append(entry)
                    self._psp["all_entries"].append(entry)
                    self._psp["kinases"].add(k)
                    self._psp["substrates"].add(s)

    def _load_signor(self):
        if self._signor is not None:
            return
        path = self.db_dir / "signor" / "signor_phospho_human.json"
        self._signor = {"by_kinase": defaultdict(list), "by_substrate": defaultdict(list),
                         "all_entries": [], "kinases": set(), "substrates": set()}
        if not path.exists():
            return
        data = json.load(open(path))
        for e in data:
            entry = {
                "kinase_gene": e.get("kinase_gene", ""),
                "substrate_gene": e.get("substrate_gene", ""),
                "phospho_site": e.get("phospho_site", ""),
                "heptameric_peptide": e.get("heptameric_peptide", ""),
                "mechanism": e.get("mechanism", ""),
                "pubmed_id": e.get("pubmed_id", ""),
                "source": "SIGNOR",
            }
            k = entry["kinase_gene"]
            s = entry["substrate_gene"]
            if k and s:
                self._signor["by_kinase"][k].append(entry)
                self._signor["by_substrate"][s].append(entry)
                self._signor["all_entries"].append(entry)
                self._signor["kinases"].add(k)
                self._signor["substrates"].add(s)

    def _load_uniprot(self):
        if self._uniprot is not None:
            return
        path = self.db_dir / "uniprot" / "uniprot_phospho_parsed.json"
        self._uniprot = {"by_kinase": defaultdict(list), "by_substrate": defaultdict(list),
                          "all_entries": [], "kinases": set(), "substrates": set()}
        if not path.exists():
            return
        data = json.load(open(path))
        for e in data:
            entry = {
                "kinase_gene": e.get("kinase_gene", ""),
                "substrate_gene": e.get("substrate_gene", ""),
                "substrate_uniprot": e.get("substrate_uniprot", ""),
                "phospho_site": e.get("phospho_site", ""),
                "source": "UniProt",
            }
            k = entry["kinase_gene"]
            s = entry["substrate_gene"]
            if k and s:
                self._uniprot["by_kinase"][k].append(entry)
                self._uniprot["by_substrate"][s].append(entry)
                self._uniprot["all_entries"].append(entry)
                self._uniprot["kinases"].add(k)
                self._uniprot["substrates"].add(s)

    def _get_db(self, db: str):
        db = db.lower().strip()
        if db in ("psp", "phosphositeplus"):
            self._load_psp()
            return self._psp
        elif db in ("signor",):
            self._load_signor()
            return self._signor
        elif db in ("uniprot", "uniprotkb"):
            self._load_uniprot()
            return self._uniprot
        else:
            return None

    # === DISCOVERY TOOLS ===

    def list_databases(self) -> dict:
        """List all available databases and their descriptions."""
        self._log_call("list_databases", {})
        return {
            "databases": [
                {
                    "id": "psp",
                    "name": "PhosphoSitePlus",
                    "description": "Curated kinase-substrate relationships with phosphorylation sites, heptameric peptides, and in vivo/in vitro evidence.",
                    "query_tools": ["list_kinases", "list_substrates", "query_by_kinase", "query_by_substrate", "query_by_site", "search", "get_stats"],
                },
                {
                    "id": "signor",
                    "name": "SIGNOR",
                    "description": "Curated signaling network with phosphorylation events, mechanisms, and PubMed references.",
                    "query_tools": ["list_kinases", "list_substrates", "query_by_kinase", "query_by_substrate", "query_by_site", "search", "get_stats"],
                },
                {
                    "id": "uniprot",
                    "name": "UniProt/UniProtKB",
                    "description": "Protein annotations including phosphorylation sites attributed to specific kinases, extracted from reviewed Swiss-Prot entries.",
                    "query_tools": ["list_kinases", "list_substrates", "query_by_kinase", "query_by_substrate", "query_by_site", "search", "get_stats"],
                },
            ]
        }

    def list_kinases(self, db: str, offset: int = 0, limit: int = 100) -> dict:
        """List all kinase gene symbols in a database, with pagination."""
        self._log_call("list_kinases", {"db": db, "offset": offset, "limit": limit})
        d = self._get_db(db)
        if d is None:
            return {"error": f"Unknown database: {db}. Use list_databases() to see available databases."}
        kinases = sorted(d["kinases"])
        return {
            "db": db,
            "total_kinases": len(kinases),
            "offset": offset,
            "limit": limit,
            "kinases": kinases[offset:offset + limit],
        }

    def list_substrates(self, db: str, offset: int = 0, limit: int = 100) -> dict:
        """List all substrate gene symbols in a database, with pagination."""
        self._log_call("list_substrates", {"db": db, "offset": offset, "limit": limit})
        d = self._get_db(db)
        if d is None:
            return {"error": f"Unknown database: {db}."}
        substrates = sorted(d["substrates"])
        return {
            "db": db,
            "total_substrates": len(substrates),
            "offset": offset,
            "limit": limit,
            "substrates": substrates[offset:offset + limit],
        }

    def get_stats(self, db: str) -> dict:
        """Get summary statistics for a database."""
        self._log_call("get_stats", {"db": db})
        d = self._get_db(db)
        if d is None:
            return {"error": f"Unknown database: {db}."}
        return {
            "db": db,
            "total_entries": len(d["all_entries"]),
            "unique_kinases": len(d["kinases"]),
            "unique_substrates": len(d["substrates"]),
        }

    def search(self, db: str, keyword: str, limit: int = 50) -> dict:
        """Search entries by keyword (matches against gene names, sites, peptides)."""
        self._log_call("search", {"db": db, "keyword": keyword, "limit": limit})
        d = self._get_db(db)
        if d is None:
            return {"error": f"Unknown database: {db}."}
        kw = keyword.upper()
        matches = []
        for e in d["all_entries"]:
            if any(kw in str(v).upper() for v in e.values()):
                matches.append(e)
                if len(matches) >= limit:
                    break
        return {
            "db": db,
            "keyword": keyword,
            "total_matches": len(matches),
            "truncated": len(matches) >= limit,
            "results": matches,
        }

    # === RETRIEVAL TOOLS ===

    def query_by_kinase(self, db: str, gene: str) -> dict:
        """Get all substrates and phospho-sites for a kinase in a database."""
        self._log_call("query_by_kinase", {"db": db, "gene": gene})
        d = self._get_db(db)
        if d is None:
            return {"error": f"Unknown database: {db}."}
        entries = d["by_kinase"].get(gene, [])
        return {
            "db": db,
            "kinase": gene,
            "total_entries": len(entries),
            "entries": entries,
        }

    def query_by_substrate(self, db: str, gene: str) -> dict:
        """Get all kinases that phosphorylate a substrate in a database."""
        self._log_call("query_by_substrate", {"db": db, "gene": gene})
        d = self._get_db(db)
        if d is None:
            return {"error": f"Unknown database: {db}."}
        entries = d["by_substrate"].get(gene, [])
        return {
            "db": db,
            "substrate": gene,
            "total_entries": len(entries),
            "entries": entries,
        }

    def query_by_site(self, db: str, gene: str, site: str) -> dict:
        """Get specific records for a gene + phospho-site combination."""
        self._log_call("query_by_site", {"db": db, "gene": gene, "site": site})
        d = self._get_db(db)
        if d is None:
            return {"error": f"Unknown database: {db}."}
        # Search both kinase and substrate indices
        matches = []
        for e in d["by_kinase"].get(gene, []):
            if e.get("phospho_site", "").upper() == site.upper():
                matches.append(e)
        for e in d["by_substrate"].get(gene, []):
            if e.get("phospho_site", "").upper() == site.upper():
                if e not in matches:
                    matches.append(e)
        return {
            "db": db,
            "gene": gene,
            "site": site,
            "total_entries": len(matches),
            "entries": matches,
        }

    # === CROSS-REFERENCE ===

    def query_all_dbs(self, gene: str) -> dict:
        """Query all databases for a gene (as kinase and substrate)."""
        self._log_call("query_all_dbs", {"gene": gene})
        results = {}
        for db_id in ("psp", "signor", "uniprot"):
            as_kinase = self.query_by_kinase(db_id, gene)
            as_substrate = self.query_by_substrate(db_id, gene)
            results[db_id] = {
                "as_kinase": as_kinase["total_entries"],
                "as_substrate": as_substrate["total_entries"],
                "kinase_entries": as_kinase["entries"],
                "substrate_entries": as_substrate["entries"],
            }
        return {"gene": gene, "databases": results}

    # === TOOL DEFINITIONS (for LLM function calling) ===

    @staticmethod
    def get_tool_definitions() -> list:
        """Return OpenAI-compatible tool definitions for function calling."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_databases",
                    "description": "List all available phosphorylation databases and their descriptions.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_kinases",
                    "description": "List all kinase gene symbols in a database. Use pagination (offset/limit) for large results.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "db": {"type": "string", "description": "Database ID (use list_databases to discover available IDs)"},
                            "offset": {"type": "integer", "description": "Start index for pagination (default 0)", "default": 0},
                            "limit": {"type": "integer", "description": "Max results to return (default 100)", "default": 100},
                        },
                        "required": ["db"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_substrates",
                    "description": "List all substrate gene symbols in a database. Use pagination for large results.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "db": {"type": "string", "description": "Database ID (use list_databases to discover available IDs)"},
                            "offset": {"type": "integer", "default": 0},
                            "limit": {"type": "integer", "default": 100},
                        },
                        "required": ["db"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_stats",
                    "description": "Get summary statistics for a database (total entries, unique kinases, unique substrates).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "db": {"type": "string", "description": "Database ID (use list_databases to discover available IDs)"},
                        },
                        "required": ["db"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search database entries by keyword. Matches against gene names, phospho-sites, peptides, and other fields.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "db": {"type": "string", "description": "Database ID (use list_databases to discover available IDs)"},
                            "keyword": {"type": "string", "description": "Search term (e.g., gene name, site like 'Y15', peptide fragment)"},
                            "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
                        },
                        "required": ["db", "keyword"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "query_by_kinase",
                    "description": "Get all substrate proteins and phosphorylation sites for a specific kinase enzyme in a database.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "db": {"type": "string", "description": "Database ID (use list_databases to discover available IDs)"},
                            "gene": {"type": "string", "description": "Kinase gene symbol (e.g., 'CDK1', 'ABL1', 'MAPK1')"},
                        },
                        "required": ["db", "gene"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "query_by_substrate",
                    "description": "Get all kinases that phosphorylate a specific substrate protein in a database.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "db": {"type": "string", "description": "Database ID (use list_databases to discover available IDs)"},
                            "gene": {"type": "string", "description": "Substrate gene symbol (e.g., 'TP53', 'RB1')"},
                        },
                        "required": ["db", "gene"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "query_by_site",
                    "description": "Get records for a specific gene + phosphorylation site combination across kinase and substrate roles.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "db": {"type": "string", "description": "Database ID (use list_databases to discover available IDs)"},
                            "gene": {"type": "string", "description": "Gene symbol"},
                            "site": {"type": "string", "description": "Phospho-site (e.g., 'Y15', 'S10', 'T161')"},
                        },
                        "required": ["db", "gene", "site"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "query_all_dbs",
                    "description": "Query ALL databases simultaneously for a gene, returning its role as kinase and/or substrate across PSP, SIGNOR, and UniProt.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "gene": {"type": "string", "description": "Gene symbol to search across all databases"},
                        },
                        "required": ["gene"],
                    },
                },
            },
        ]

    def dispatch(self, tool_name: str, arguments: dict) -> dict:
        """Dispatch a tool call by name. Used by agent runners."""
        fn = getattr(self, tool_name, None)
        if fn is None:
            return {"error": f"Unknown tool: {tool_name}. Available: list_databases, list_kinases, list_substrates, get_stats, search, query_by_kinase, query_by_substrate, query_by_site, query_all_dbs"}
        return fn(**arguments)


# === REST API server (optional, for remote agents) ===

def serve(port: int = 8000, db_dir: str = "databases"):
    """Start a simple REST API server for the database tools."""
    try:
        from http.server import HTTPServer, BaseHTTPRequestHandler
    except ImportError:
        print("http.server not available")
        return

    tools = DatabaseTools(db_dir)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            tool_name = self.path.strip("/")
            result = tools.dispatch(tool_name, body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        def do_GET(self):
            if self.path == "/tools":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(DatabaseTools.get_tool_definitions()).encode())
            else:
                self.send_response(404)
                self.end_headers()

    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Database tools server running on http://0.0.0.0:{port}")
    print(f"  GET  /tools          -> tool definitions")
    print(f"  POST /list_databases -> list databases")
    print(f"  POST /query_by_kinase {{\"db\":\"psp\",\"gene\":\"CDK1\"}} -> query")
    server.serve_forever()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", help="Start REST API server")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db-dir", default="databases")
    parser.add_argument("--test", action="store_true", help="Quick self-test")
    args = parser.parse_args()

    if args.serve:
        serve(args.port, args.db_dir)
    elif args.test:
        tools = DatabaseTools(args.db_dir)
        print("=== Self-test ===")
        dbs = tools.list_databases()
        print(f"Databases: {[d['id'] for d in dbs['databases']]}")
        for db_id in ("psp", "signor", "uniprot"):
            stats = tools.get_stats(db_id)
            print(f"  {db_id}: {stats['total_entries']} entries, {stats['unique_kinases']} kinases")
        cdk1 = tools.query_by_kinase("psp", "CDK1")
        print(f"\nCDK1 in PSP: {cdk1['total_entries']} substrates")
        if cdk1["entries"]:
            e = cdk1["entries"][0]
            print(f"  First: {e['substrate_gene']} {e['phospho_site']} {e.get('heptameric_peptide','')}")
        cross = tools.query_all_dbs("CDK1")
        for db_id, r in cross["databases"].items():
            print(f"  CDK1 in {db_id}: {r['as_kinase']} as kinase, {r['as_substrate']} as substrate")
        print(f"\nTotal tool calls: {tools.call_count}")
        print("=== PASS ===")
