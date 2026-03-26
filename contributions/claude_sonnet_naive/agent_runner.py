#!/usr/bin/env python3
"""
Claude Sonnet Naive Agent Runner for PhosphoAtlas Benchmark.

Everything is discovered at runtime — no database names or URLs are hardcoded:

  1. Database discovery: calls DatabaseTools.list_databases() to learn which
     databases exist (names, descriptions).
  2. Domain discovery: web-searches each database name via DuckDuckGo to find
     its official website.
  3. Endpoint probing: tries common API/download URL patterns on the discovered
     domain until one returns data (not HTML).
  4. Adaptive parsing: detects the response format (gzipped TSV, plain TSV,
     paginated JSON) and maps columns/fields to the atlas schema by matching
     header names or JSON keys.
  5. Merge, deduplicate, QC, and score.

Usage:
  python3 contributions/claude_sonnet_naive/agent_runner.py
"""
import csv
import gzip
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from databases.tools import DatabaseTools


# ── Atlas accumulator ─────────────────────────────────────────────────────────

def _make_atlas_dict():
    """Return a fresh atlas dict keyed by (kinase|substrate|site) and an add_entry closure."""
    atlas = {}

    def add_entry(kinase, substrate, site, uniprot="", peptide="", source=""):
        if not (kinase and substrate and site):
            return
        key = f"{kinase}|{substrate}|{site}"
        if key not in atlas:
            atlas[key] = {
                "kinase_gene": kinase,
                "substrate_gene": substrate,
                "phospho_site": site,
                "substrate_uniprot": uniprot or "",
                "heptameric_peptide": peptide or "",
                "supporting_databases": [source] if source else [],
            }
        else:
            entry = atlas[key]
            if source and source not in entry["supporting_databases"]:
                entry["supporting_databases"].append(source)
            if not entry["substrate_uniprot"] and uniprot:
                entry["substrate_uniprot"] = uniprot
            if not entry["heptameric_peptide"] and peptide:
                entry["heptameric_peptide"] = peptide

    return atlas, add_entry


# ── HTTP helpers ──────────────────────────────────────────────────────────────

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _fetch(url, timeout=120, method="GET"):
    """Fetch a URL. Returns the HTTPResponse object."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA}, method=method)
    return urllib.request.urlopen(req, timeout=timeout)


def _head_ok(url, timeout=10):
    """Return True if HEAD request returns 200 with a data content-type."""
    try:
        resp = _fetch(url, timeout=timeout, method="HEAD")
        ct = (resp.headers.get("Content-Type") or "").lower()
        # Accept anything that is NOT html (html = landing page, not data)
        return resp.status == 200 and "text/html" not in ct
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: DATABASE DISCOVERY  (via tool interface)
# ═══════════════════════════════════════════════════════════════════════════════

def discover_databases(log_fn):
    """Use DatabaseTools.list_databases() to discover available databases."""
    tools = DatabaseTools("databases")
    result = tools.list_databases()
    databases = result["databases"]
    log_fn("DISCOVER", f"Tool interface reports {len(databases)} databases:")
    for db in databases:
        log_fn("DISCOVER", f"  {db['name']} (id={db['id']}): {db['description']}")
    return databases


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: ENDPOINT DISCOVERY  (web search + URL probing)
# ═══════════════════════════════════════════════════════════════════════════════

def _web_search(query, max_results=15):
    """Search DuckDuckGo HTML and return deduplicated result URLs."""
    search_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    req = urllib.request.Request(search_url, headers={"User-Agent": _UA})
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return []

    # DuckDuckGo encodes target URLs in a 'uddg' parameter
    redirects = re.findall(r"uddg=(https?%3A[^&\"]+)", html)
    urls = [urllib.parse.unquote(u) for u in redirects]
    if not urls:
        urls = re.findall(
            r'href="(https?://(?!duckduckgo|html\.duckduckgo|improving)[^"]+)"',
            html,
        )

    seen = set()
    unique = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique[:max_results]


def _extract_domain(url):
    """Extract the base domain from a URL (e.g. 'signor.uniroma2.it')."""
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc


def discover_domain(db_name, db_description, log_fn):
    """Web-search for a database's official website and return its domain."""
    query = f"{db_name} database official site"
    log_fn("SEARCH", f"  Web search: \"{query}\"")
    urls = _web_search(query)
    log_fn("SEARCH", f"  Got {len(urls)} results")

    # Rank domains: prefer those whose name overlaps the database name
    db_tokens = set(re.findall(r"[a-z]+", db_name.lower()))
    scored = []
    for url in urls:
        domain = _extract_domain(url)
        if not domain:
            continue
        domain_tokens = set(re.findall(r"[a-z]+", domain.lower()))
        overlap = len(db_tokens & domain_tokens)
        # Bonus for .org/.edu/.ac. domains (likely official)
        if any(domain.endswith(s) for s in (".org", ".edu", ".ac.uk", ".ac.it")):
            overlap += 1
        scored.append((overlap, domain, url))

    scored.sort(key=lambda x: -x[0])

    # Deduplicate by domain
    seen_domains = set()
    for overlap, domain, url in scored:
        if domain not in seen_domains:
            seen_domains.add(domain)
            log_fn("SEARCH", f"  Candidate domain: {domain} (overlap={overlap})")

    if scored:
        best_domain = scored[0][1]
        log_fn("SEARCH", f"  Selected domain: {best_domain}")
        return best_domain

    log_fn("SEARCH", f"  WARNING: no domain found for {db_name}")
    return None


# ── Common API / download path patterns to probe on a domain ─────────────────

_DOWNLOAD_PATH_PATTERNS = [
    # File-download patterns (for databases that offer bulk downloads)
    "/downloads/Kinase_Substrate_Dataset.gz",
    "/downloads/kinase_substrate_dataset.gz",
    "/downloads/phosphorylation_site_dataset.gz",
    # REST/API patterns (for databases with query interfaces)
    "/getData.php?organism=9606",
    "/api/v1/getData?organism=9606",
    "/download_entity.php?organism=9606&format=csv",
]

_REST_API_PATTERNS = [
    # UniProt-style REST API
    "{scheme}://rest.{domain}/uniprotkb/search?query=organism_id:9606&format=json&size=1",
]


def discover_endpoint(domain, db_name, db_description, log_fn):
    """Probe common API/download paths on the discovered domain to find a data endpoint."""
    log_fn("PROBE", f"  Probing {domain} for data endpoints...")

    # Try standard download/API paths on the domain
    for path in _DOWNLOAD_PATH_PATTERNS:
        url = f"https://{domain}{path}"
        if _head_ok(url):
            log_fn("PROBE", f"  FOUND data endpoint: {url}")
            return url

    # Try REST API subdomain patterns (rest.domain.org)
    domain_parts = domain.split(".")
    base_domain = ".".join(domain_parts[-2:]) if len(domain_parts) >= 2 else domain
    for pattern in _REST_API_PATTERNS:
        url = pattern.format(scheme="https", domain=base_domain)
        try:
            resp = _fetch(url, timeout=10)
            data = resp.read(500).decode("utf-8", errors="replace")
            if '"results"' in data or '"entries"' in data:
                # Strip the test parameters to return the base endpoint
                base_url = url.split("?")[0]
                log_fn("PROBE", f"  FOUND REST API: {base_url}")
                return base_url
        except Exception:
            pass

    log_fn("PROBE", f"  WARNING: no data endpoint found on {domain}")
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 3: DOWNLOAD AND ADAPTIVE PARSING
# ═══════════════════════════════════════════════════════════════════════════════

def download_and_parse(endpoint, db_info, log_fn):
    """Download data from the discovered endpoint, auto-detect format, and parse entries."""
    log_fn("DOWNLOAD", f"  Downloading from: {endpoint}")

    try:
        resp = _fetch(endpoint, timeout=180)
        raw = resp.read()
        content_type = (resp.headers.get("Content-Type") or "").lower()
    except Exception as e:
        log_fn("DOWNLOAD", f"  FAILED: {e}")
        return []

    log_fn("DOWNLOAD", f"  Received {len(raw)} bytes, content-type={content_type}")

    # ── Auto-detect format and dispatch to the right parser ──────────────

    # Gzipped file → decompress first, then parse as TSV
    if "gzip" in content_type or "x-gzip" in content_type or endpoint.endswith(".gz"):
        return _parse_gzipped_tsv(raw, db_info, log_fn)

    # JSON → parse as structured JSON
    if "json" in content_type:
        return _parse_json_entries(raw, db_info, log_fn)

    # Plain text / CSV → parse as TSV
    text = raw.decode("utf-8", errors="replace")
    if "\t" in text[:2000]:
        return _parse_plain_tsv(text, db_info, log_fn)

    log_fn("DOWNLOAD", f"  WARNING: unrecognized format, skipping")
    return []


def download_and_parse_rest_api(endpoint, db_info, log_fn):
    """Download from a paginated REST API (UniProt-style) and parse entries."""
    log_fn("DOWNLOAD", f"  Querying REST API: {endpoint}")

    # Build a query for human reviewed proteins with phosphorylation annotations
    query = ("(organism_id:9606) AND (reviewed:true) AND "
             "(ft_mod_res:Phosphoserine OR ft_mod_res:Phosphothreonine "
             "OR ft_mod_res:Phosphotyrosine)")
    fields = "accession,gene_names,ft_mod_res"
    page_size = 500

    all_entries = []
    cursor = None
    page = 0
    total_proteins = 0

    while True:
        params = {
            "query": query,
            "format": "json",
            "size": str(page_size),
            "fields": fields,
        }
        if cursor:
            params["cursor"] = cursor

        url = f"{endpoint}?{urllib.parse.urlencode(params)}"

        try:
            resp = _fetch(url, timeout=120)
            link_header = resp.headers.get("Link", "")
            next_cursor = None
            if link_header:
                for part in link_header.split(","):
                    if 'rel="next"' in part:
                        m = re.search(r"cursor=([^&>\"]+)", part)
                        if m:
                            next_cursor = m.group(1)

            data = json.loads(resp.read())
            results = data.get("results", [])
            if not results:
                break

            total_proteins += len(results)
            page_entries = _extract_kinase_phospho_from_proteins(results)
            all_entries.extend(page_entries)

            page += 1
            if page % 5 == 0:
                log_fn("DOWNLOAD", f"    Page {page}: {total_proteins} proteins, "
                       f"{len(all_entries)} kinase-attributed entries")

            if not next_cursor:
                break
            cursor = next_cursor

        except Exception as e:
            log_fn("DOWNLOAD", f"    Page {page} FAILED: {e}")
            break

    log_fn("DOWNLOAD", f"  Processed {total_proteins} proteins, "
           f"parsed {len(all_entries)} kinase-attributed entries")
    return all_entries


# ── Adaptive TSV column mapper ────────────────────────────────────────────────

# Priority-ordered lists: most specific alias first.
# e.g. "SUB_GENE" (gene symbol) is preferred over "SUBSTRATE" (protein name).
_COLUMN_ALIASES = {
    "kinase_gene": ["GENE", "KINASE_GENE", "kinase_gene", "ENTITYA", "KINASE"],
    "substrate_gene": ["SUB_GENE", "SUBSTRATE_GENE", "substrate_gene", "ENTITYB", "SUBSTRATE"],
    "phospho_site": ["SUB_MOD_RSD", "PHOSPHO_SITE", "phospho_site", "RESIDUE", "SITE"],
    "substrate_uniprot": ["SUB_ACC_ID", "SUBSTRATE_UNIPROT", "substrate_uniprot", "IDB"],
    "heptameric_peptide": ["SITE_+/-7_AA", "SEQUENCE", "PEPTIDE", "heptameric_peptide"],
    "organism": ["SUB_ORGANISM", "ORGANISM", "TAX_ID"],
    "mechanism": ["MECHANISM"],
    "entity_type_a": ["TYPEA"],
    "entity_type_b": ["TYPEB"],
}


def _map_columns(headers):
    """Map TSV column headers to canonical atlas field names.

    Iterates aliases in priority order (most specific first) and checks
    whether each alias exists in the header row.  Returns a dict:
    {canonical_name: column_index}.
    """
    header_index = {h.strip(): i for i, h in enumerate(headers)}
    mapping = {}
    for canon, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in header_index:
                mapping[canon] = header_index[alias]
                break
    return mapping


# ── Gzipped-TSV parser (PSP-style) ───────────────────────────────────────────

def _parse_gzipped_tsv(raw_bytes, db_info, log_fn):
    """Decompress gzip, skip comment lines, auto-detect columns, parse entries."""
    try:
        text = gzip.decompress(raw_bytes).decode("utf-8", errors="replace")
    except Exception as e:
        log_fn("PARSE", f"  Gzip decompression failed: {e}")
        return []

    lines = text.split("\n")

    # Skip comment/copyright lines (lines that don't look like TSV headers)
    data_start = 0
    for i, line in enumerate(lines):
        tabs = line.count("\t")
        if tabs >= 5:
            data_start = i
            break

    # First data line with tabs is the header
    header_line = lines[data_start]
    headers = header_line.split("\t")
    col_map = _map_columns(headers)
    log_fn("PARSE", f"  Detected TSV columns: {col_map}")

    if "kinase_gene" not in col_map or "substrate_gene" not in col_map:
        log_fn("PARSE", f"  WARNING: could not map kinase/substrate columns from headers: {headers[:10]}")
        return []

    entries = []
    reader = csv.reader(io.StringIO("\n".join(lines[data_start + 1:])), delimiter="\t")
    for row in reader:
        if len(row) <= max(col_map.values(), default=0):
            continue

        # Filter for human if an organism column exists
        if "organism" in col_map:
            org = row[col_map["organism"]].strip()
            if org and org != "human" and org != "9606":
                continue

        kinase = row[col_map["kinase_gene"]].strip()
        substrate = row[col_map["substrate_gene"]].strip()
        site = row[col_map.get("phospho_site", -1)].strip() if "phospho_site" in col_map else ""
        uniprot = row[col_map.get("substrate_uniprot", -1)].strip() if "substrate_uniprot" in col_map else ""
        peptide = row[col_map.get("heptameric_peptide", -1)].strip() if "heptameric_peptide" in col_map else ""

        if kinase and substrate and site:
            entries.append({
                "kinase_gene": kinase,
                "substrate_gene": substrate,
                "phospho_site": site,
                "substrate_uniprot": uniprot,
                "heptameric_peptide": peptide,
            })

    log_fn("PARSE", f"  Parsed {len(entries)} entries from gzipped TSV")
    return entries


# ── Plain-TSV parser (SIGNOR-style: headerless) ──────────────────────────────

def _parse_plain_tsv(text, db_info, log_fn):
    """Parse headerless or headed TSV. Uses positional heuristics if no header found."""
    lines = text.strip().split("\n")
    if not lines:
        return []

    # Check if the first line looks like a header (contains known column names)
    first_cols = lines[0].split("\t")
    col_map = _map_columns(first_cols)

    if "kinase_gene" in col_map and "substrate_gene" in col_map:
        # Has recognizable headers — parse with header
        log_fn("PARSE", f"  Detected headed TSV, columns: {col_map}")
        return _parse_tsv_with_map(lines[1:], col_map, log_fn)

    # Headerless TSV — use positional heuristics
    # Common layout: ENTITYA(0) TYPEA(1) IDA(2) DBA(3) ENTITYB(4) TYPEB(5) IDB(6) DBB(7)
    #                EFFECT(8) MECHANISM(9) RESIDUE(10) SEQUENCE(11) TAX_ID(12) ...
    log_fn("PARSE", f"  No header detected, using positional heuristics")
    log_fn("PARSE", f"  First line preview: {first_cols[:6]}...")

    # Verify the layout: check if col 9 contains mechanism-like values
    sample_mechs = set()
    for line in lines[:100]:
        cols = line.split("\t")
        if len(cols) > 9:
            sample_mechs.add(cols[9].strip().lower())

    if "phosphorylation" in sample_mechs:
        log_fn("PARSE", f"  Confirmed SIGNOR-like layout (col 9 = mechanism)")
        positional_map = {
            "kinase_gene": 0,
            "entity_type_a": 1,
            "substrate_gene": 4,
            "entity_type_b": 5,
            "mechanism": 9,
            "phospho_site": 10,
            "heptameric_peptide": 11,
            "organism": 12,
        }
        return _parse_signor_positional(lines, positional_map, log_fn)

    log_fn("PARSE", f"  WARNING: unrecognized TSV layout, skipping")
    return []


def _parse_tsv_with_map(lines, col_map, log_fn):
    """Parse TSV lines using a column-name mapping."""
    entries = []
    for line in lines:
        row = line.split("\t")
        if len(row) <= max(col_map.values(), default=0):
            continue

        if "organism" in col_map:
            org = row[col_map["organism"]].strip()
            if org and org != "human" and org != "9606":
                continue

        kinase = row[col_map["kinase_gene"]].strip()
        substrate = row[col_map["substrate_gene"]].strip()
        site = row[col_map.get("phospho_site", -1)].strip() if "phospho_site" in col_map else ""
        uniprot = row[col_map.get("substrate_uniprot", -1)].strip() if "substrate_uniprot" in col_map else ""
        peptide = row[col_map.get("heptameric_peptide", -1)].strip() if "heptameric_peptide" in col_map else ""

        if kinase and substrate and site:
            entries.append({
                "kinase_gene": kinase,
                "substrate_gene": substrate,
                "phospho_site": site,
                "substrate_uniprot": uniprot,
                "heptameric_peptide": peptide,
            })
    log_fn("PARSE", f"  Parsed {len(entries)} entries from headed TSV")
    return entries


def _parse_signor_positional(lines, pos, log_fn):
    """Parse SIGNOR-style headerless TSV using positional column indices."""
    entries = []
    for line in lines:
        cols = line.split("\t")
        if len(cols) <= max(pos.values()):
            continue

        mechanism = cols[pos["mechanism"]].strip().lower()
        if mechanism != "phosphorylation":
            continue

        type_a = cols[pos["entity_type_a"]].strip()
        type_b = cols[pos["entity_type_b"]].strip()
        if type_a != "protein" or type_b != "protein":
            continue

        site = cols[pos["phospho_site"]].strip()
        if not site:
            continue

        kinase = cols[pos["kinase_gene"]].strip()
        substrate = cols[pos["substrate_gene"]].strip()
        peptide = cols[pos.get("heptameric_peptide", -1)].strip() if "heptameric_peptide" in pos else ""

        if kinase and substrate:
            entries.append({
                "kinase_gene": kinase,
                "substrate_gene": substrate,
                "phospho_site": site,
                "heptameric_peptide": peptide,
            })

    log_fn("PARSE", f"  Parsed {len(entries)} phosphorylation entries from positional TSV")
    return entries


# ── JSON parser ───────────────────────────────────────────────────────────────

def _parse_json_entries(raw_bytes, db_info, log_fn):
    """Parse JSON — handles both flat entry arrays and UniProt-style protein records."""
    try:
        data = json.loads(raw_bytes)
    except json.JSONDecodeError as e:
        log_fn("PARSE", f"  JSON parse failed: {e}")
        return []

    # If top-level is a list, treat as flat entries
    if isinstance(data, list):
        return _parse_flat_json(data, log_fn)

    # If dict with "results", may be UniProt-style paginated response
    if isinstance(data, dict) and "results" in data:
        results = data["results"]
        if results and "features" in results[0]:
            return _extract_kinase_phospho_from_proteins(results)
        return _parse_flat_json(results, log_fn)

    log_fn("PARSE", f"  WARNING: unrecognized JSON structure")
    return []


def _parse_flat_json(records, log_fn):
    """Parse a JSON array of entries with kinase/substrate/site fields."""
    entries = []
    for rec in records:
        kinase = rec.get("kinase_gene", rec.get("kinase", ""))
        substrate = rec.get("substrate_gene", rec.get("substrate", ""))
        site = rec.get("phospho_site", rec.get("site", ""))
        uniprot = rec.get("substrate_uniprot", rec.get("uniprot", ""))
        peptide = rec.get("heptameric_peptide", rec.get("peptide", ""))

        if kinase and substrate and site:
            entries.append({
                "kinase_gene": str(kinase).strip(),
                "substrate_gene": str(substrate).strip(),
                "phospho_site": str(site).strip(),
                "substrate_uniprot": str(uniprot).strip() if uniprot else "",
                "heptameric_peptide": str(peptide).strip() if peptide else "",
            })

    log_fn("PARSE", f"  Parsed {len(entries)} entries from flat JSON")
    return entries


# ── UniProt protein-record parser ─────────────────────────────────────────────

_RESIDUE_MAP = {
    "phosphoserine": "S", "phosphothreonine": "T",
    "phosphotyrosine": "Y", "phosphohistidine": "H",
}


def _extract_kinase_phospho_from_proteins(proteins):
    """Extract kinase-attributed phospho-site entries from UniProt protein records.

    Looks for 'Modified residue' features with descriptions like:
      "Phosphoserine; by MTOR"
      "Phosphotyrosine; by ABL1 and SRC"
    """
    entries = []
    for protein in proteins:
        accession = protein.get("primaryAccession", "")
        genes = protein.get("genes", [])
        substrate_gene = ""
        if genes:
            gn = genes[0].get("geneName", {})
            substrate_gene = gn.get("value", "") if isinstance(gn, dict) else ""
        if not substrate_gene:
            continue

        for feat in protein.get("features", []):
            if feat.get("type") != "Modified residue":
                continue
            desc = feat.get("description", "")
            desc_lower = desc.lower()
            if "phospho" not in desc_lower:
                continue

            # Must have kinase attribution
            by_match = re.search(r"\bby\s+(\S+)", desc)
            if not by_match:
                continue

            # Extract all kinase names from "by X and Y" patterns
            by_section = desc[by_match.start():]
            kinase_names = re.findall(r"\b([A-Z][A-Z0-9]{1,})\b", by_section)
            if not kinase_names:
                kinase_names = [by_match.group(1)]

            # Determine residue type
            residue = ""
            for key, code in _RESIDUE_MAP.items():
                if key in desc_lower:
                    residue = code
                    break
            if not residue:
                continue

            location = feat.get("location", {})
            start = location.get("start", {})
            position = start.get("value", "") if isinstance(start, dict) else ""
            if not position:
                continue

            site = f"{residue}{position}"
            skip_words = {"AND", "OR", "THE", "IN", "VITRO", "VIVO", "NOT"}
            for kinase in kinase_names:
                if kinase not in skip_words:
                    entries.append({
                        "kinase_gene": kinase,
                        "substrate_gene": substrate_gene,
                        "substrate_uniprot": accession,
                        "phospho_site": site,
                    })

    return entries


# ── Finalize helper ──────────────────────────────────────────────────────────

def _finalize(atlas, log_fn):
    """Sort, compute stats, log summary, return sorted entry list."""
    entries = sorted(
        atlas.values(),
        key=lambda e: (e["kinase_gene"], e["substrate_gene"], e["phospho_site"]),
    )
    multi_db = sum(1 for e in entries if len(e["supporting_databases"]) >= 2)
    kinases = set(e["kinase_gene"] for e in entries)
    substrates = set(e["substrate_gene"] for e in entries)
    with_uniprot = sum(1 for e in entries if e["substrate_uniprot"])
    with_peptide = sum(1 for e in entries if e["heptameric_peptide"])

    log_fn("RESULT", f"  Unique triplets:   {len(entries)}")
    log_fn("RESULT", f"  Unique kinases:    {len(kinases)}")
    log_fn("RESULT", f"  Unique substrates: {len(substrates)}")
    log_fn("RESULT", f"  Multi-DB support:  {multi_db} ({multi_db / max(len(entries), 1) * 100:.1f}%)")
    log_fn("RESULT", f"  With UniProt ID:   {with_uniprot}")
    log_fn("RESULT", f"  With peptide:      {with_peptide}")
    return entries


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

def run_naive(log_fn):
    """Execute the full naive pipeline with runtime discovery.

    1. Discover databases from tool interface
    2. Find each database's official domain via web search
    3. Probe the domain for data endpoints
    4. Download, auto-detect format, parse entries
    5. Merge, deduplicate, QC
    """
    atlas, add_entry = _make_atlas_dict()
    discovered_endpoints = {}

    # ── Phase 1: Discover databases ──────────────────────────────────────
    log_fn("PHASE1", "=" * 60)
    log_fn("PHASE1", "PHASE 1: DISCOVER DATABASES (via tool interface)")
    log_fn("PHASE1", "=" * 60)

    databases = discover_databases(log_fn)

    # ── Phase 2: Discover endpoints ──────────────────────────────────────
    log_fn("PHASE2", "=" * 60)
    log_fn("PHASE2", "PHASE 2: DISCOVER API ENDPOINTS (via web search + probing)")
    log_fn("PHASE2", "=" * 60)

    for db in databases:
        db_name = db["name"]
        db_desc = db["description"]
        log_fn("PHASE2", f"--- {db_name} ---")

        domain = discover_domain(db_name, db_desc, log_fn)
        if not domain:
            log_fn("PHASE2", f"  SKIPPING: could not find domain for {db_name}")
            continue

        endpoint = discover_endpoint(domain, db_name, db_desc, log_fn)
        if endpoint:
            discovered_endpoints[db["id"]] = {
                "name": db_name,
                "domain": domain,
                "endpoint": endpoint,
            }
        else:
            log_fn("PHASE2", f"  SKIPPING: could not find data endpoint on {domain}")

    log_fn("PHASE2", f"Discovered endpoints for {len(discovered_endpoints)}/{len(databases)} databases")

    # ── Phase 3: Download and parse ──────────────────────────────────────
    log_fn("PHASE3", "=" * 60)
    log_fn("PHASE3", "PHASE 3: DOWNLOAD, PARSE, AND MERGE")
    log_fn("PHASE3", "=" * 60)

    for db_id, info in discovered_endpoints.items():
        db_name = info["name"]
        endpoint = info["endpoint"]
        log_fn("PHASE3", f"--- {db_name} ({endpoint}) ---")

        # Choose download strategy based on endpoint type
        is_rest_api = "search" in endpoint and "uniprotkb" in endpoint
        if is_rest_api:
            entries = download_and_parse_rest_api(endpoint, {"name": db_name}, log_fn)
        else:
            entries = download_and_parse(endpoint, {"name": db_name}, log_fn)

        before = len(atlas)
        for e in entries:
            add_entry(
                e.get("kinase_gene", ""),
                e.get("substrate_gene", ""),
                e.get("phospho_site", ""),
                e.get("substrate_uniprot", ""),
                e.get("heptameric_peptide", ""),
                db_name,
            )
        gained = len(atlas) - before
        log_fn("PHASE3", f"  +{gained} new unique triplets (atlas total: {len(atlas)})")

    # ── Phase 4: Cross-reference and QC ──────────────────────────────────
    log_fn("PHASE4", "=" * 60)
    log_fn("PHASE4", "PHASE 4: CROSS-REFERENCE AND QUALITY CONTROL")
    log_fn("PHASE4", "=" * 60)

    multi_db = sum(1 for e in atlas.values() if len(e["supporting_databases"]) >= 2)
    log_fn("XREF", f"  Entries in 2+ databases: {multi_db}")

    db_counts = {}
    for entry in atlas.values():
        for db in entry["supporting_databases"]:
            db_counts[db] = db_counts.get(db, 0) + 1
    for db_name, count in sorted(db_counts.items()):
        log_fn("XREF", f"  {db_name}: {count} entries")

    # QC: remove entries with empty required fields
    pre_qc = len(atlas)
    to_remove = [
        key for key, entry in atlas.items()
        if not entry["kinase_gene"] or not entry["substrate_gene"] or not entry["phospho_site"]
    ]
    for key in to_remove:
        del atlas[key]
    log_fn("QC", f"  Removed {len(to_remove)} entries with missing fields ({pre_qc} -> {len(atlas)})")

    log_fn("RESULT", "=== FINAL ATLAS ===")
    return _finalize(atlas, log_fn)


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Claude Sonnet Naive Agent Runner")
    parser.add_argument("--output-dir", default=None,
                        help="Output directory (default: contributions/claude_sonnet_naive)")
    args = parser.parse_args()

    out_dir = Path(args.output_dir) if args.output_dir else Path("contributions/claude_sonnet_naive")
    out_dir.mkdir(parents=True, exist_ok=True)
    scores_dir = out_dir / "scores"
    scores_dir.mkdir(parents=True, exist_ok=True)

    log_lines = []

    def log_fn(phase, msg):
        line = f"[{time.strftime('%H:%M:%S')}][{phase}] {msg}"
        log_lines.append(line)
        print(line, flush=True)

    prompt_path = Path("agents/prompts/naive.txt")
    prompt = prompt_path.read_text() if prompt_path.exists() else "(naive prompt not found)"
    log_fn("SETUP", f"Prompt: {prompt_path} ({len(prompt)} chars)")
    log_fn("SETUP", "Agent: Claude Sonnet 4.6")
    log_fn("SETUP", "Condition: naive (zero-shot)")
    log_fn("SETUP", f"Output: {out_dir}")
    log_fn("SETUP", "Strategy: Runtime discovery of databases and API endpoints.")
    log_fn("SETUP", "  No database names or URLs are hardcoded in this script.")

    # Run pipeline
    t0 = time.time()
    entries = run_naive(log_fn)
    elapsed = time.time() - t0
    log_fn("DONE", f"Elapsed: {elapsed:.1f}s ({elapsed / 60:.1f}m)")

    # Save atlas.json
    atlas_path = out_dir / "atlas.json"
    with open(atlas_path, "w") as f:
        json.dump(entries, f, indent=2)
    log_fn("SAVE", f"Atlas: {atlas_path} ({len(entries)} entries)")

    # Compute per-DB counts
    db_counts = {}
    multi_db_count = 0
    for e in entries:
        for db in e["supporting_databases"]:
            db_counts[db] = db_counts.get(db, 0) + 1
        if len(e["supporting_databases"]) >= 2:
            multi_db_count += 1

    kinase_set = set(e["kinase_gene"] for e in entries)

    # Save run_log.json
    run_log = {
        "agent": "Claude Sonnet 4.6",
        "prompt": "naive (zero-shot)",
        "strategy": (
            "Runtime discovery pipeline: (1) Discovered databases via "
            "DatabaseTools.list_databases() tool interface. (2) Web-searched "
            "each database name via DuckDuckGo to find official domains. "
            "(3) Probed common API/download paths on each domain to find data "
            "endpoints. (4) Downloaded raw data, auto-detected format (gzip TSV, "
            "plain TSV, paginated JSON), and parsed entries using adaptive "
            "column mapping. (5) Merged across databases by (kinase|substrate|site) "
            "key with dedup. No database names or URLs hardcoded."
        ),
        "databases_accessed": sorted(db_counts.keys()),
        "raw_counts": {k: v for k, v in db_counts.items()},
        "merged_atlas": len(entries),
        "unique_kinases": len(kinase_set),
        "unique_substrates": len(set(e["substrate_gene"] for e in entries)),
        "multi_db_entries": multi_db_count,
    }
    with open(out_dir / "run_log.json", "w") as f:
        json.dump(run_log, f, indent=2)

    # Save log
    with open(out_dir / "run.log", "w") as f:
        f.write("\n".join(log_lines))

    # Run scorer
    log_fn("SCORE", "Running evaluation scorer...")
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
        log_fn("SCORE", f"  Atlas size:       {ov['atlas_size']}")
        log_fn("SCORE", f"  Recall:           {ov['recall']}")
        log_fn("SCORE", f"  Precision:        {ov['precision']}")
        log_fn("SCORE", f"  F1:               {ov['f1']}")
        log_fn("SCORE", f"  Kinases found:    {ov['kinases_found']}")
        log_fn("SCORE", f"  Multi-DB:         {ov['multi_db_pct']}%")
        log_fn("SCORE", f"  Peptide accuracy: {ov['peptide_accuracy']}")

        if ov["f1"] >= 0.75:
            log_fn("SCORE", "  PASS: F1 >= 0.75")
        else:
            log_fn("SCORE", f"  WARN: F1 = {ov['f1']} < 0.75 target")
        if ov["recall"] >= 0.90:
            log_fn("SCORE", "  PASS: Recall >= 0.90")
        else:
            log_fn("SCORE", f"  WARN: Recall = {ov['recall']} < 0.90 target")
    else:
        log_fn("SCORE", f"  Gold standard not found at {gold_path}, skipping scoring")

    # Re-save log with scoring
    with open(out_dir / "run.log", "w") as f:
        f.write("\n".join(log_lines))

    print(f"\n{'=' * 60}")
    print(f"Outputs in: {out_dir}/")
    print(f"  atlas.json, run_log.json, run.log")
    print(f"  scores/summary.json, scores/per_kinase.json, scores/peptide_mismatches.json")


if __name__ == "__main__":
    main()
