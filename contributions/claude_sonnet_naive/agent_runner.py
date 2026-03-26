#!/usr/bin/env python3
"""
Claude Sonnet Naive Agent Runner for PhosphoAtlas Benchmark.

Implements the naive (zero-shot) condition by independently discovering
and querying public database APIs for PhosphoSitePlus, SIGNOR, and UniProt
to build a comprehensive human kinase-substrate-phosphosite atlas.

Discovered API endpoints:
  PSP:    https://www.phosphosite.org/downloads/Kinase_Substrate_Dataset.gz
  SIGNOR: https://signor.uniroma2.it/getData.php?organism=9606
  UniProt: https://rest.uniprot.org/uniprotkb/search (reviewed human phosphoproteins)

Strategy (3 phases, derived purely from the naive prompt):
  Phase 1: Discover databases and download raw data from public APIs
  Phase 2: Parse each database into kinase-substrate-site entries
  Phase 3: Merge across databases with deduplication, cross-reference, and QC

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

# Ensure repo root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


# ── Deduplication accumulator ─────────────────────────────────────────────────

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


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _fetch_url(url, timeout=120):
    """Fetch URL content with a reasonable User-Agent."""
    headers = {"User-Agent": "PhosphoAtlas-Agent/1.0 (benchmark research)"}
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


# ── Phase 2a: Download and parse PhosphoSitePlus ─────────────────────────────

def fetch_psp(log_fn):
    """Download PSP Kinase_Substrate_Dataset.gz and parse human entries.

    Endpoint: https://www.phosphosite.org/downloads/Kinase_Substrate_Dataset.gz
    Format: gzipped TSV with 3 header/comment lines, then column headers, then data.
    Filter: SUB_ORGANISM == 'human'
    """
    url = "https://www.phosphosite.org/downloads/Kinase_Substrate_Dataset.gz"
    log_fn("PSP", f"Downloading from: {url}")

    try:
        response = _fetch_url(url, timeout=60)
        raw = gzip.decompress(response.read())
        text = raw.decode("utf-8", errors="replace")
    except Exception as e:
        log_fn("PSP", f"DOWNLOAD FAILED: {e}")
        log_fn("PSP", "Note: PSP may require registration/terms agreement")
        return []

    # Skip first 3 lines (copyright/attribution), then parse TSV
    lines = text.split("\n")
    data_start = "\n".join(lines[3:])
    reader = csv.DictReader(io.StringIO(data_start), delimiter="\t")

    entries = []
    for row in reader:
        if row.get("SUB_ORGANISM", "").strip() != "human":
            continue
        kinase = row.get("GENE", "").strip()
        substrate = row.get("SUB_GENE", "").strip()
        site = row.get("SUB_MOD_RSD", "").strip()
        if not (kinase and substrate and site):
            continue
        entries.append({
            "kinase_gene": kinase,
            "substrate_gene": substrate,
            "phospho_site": site,
            "substrate_uniprot": row.get("SUB_ACC_ID", "").strip(),
            "heptameric_peptide": row.get("SITE_+/-7_AA", "").strip(),
        })

    log_fn("PSP", f"Parsed {len(entries)} human kinase-substrate entries")
    return entries


# ── Phase 2b: Download and parse SIGNOR ───────────────────────────────────────

def fetch_signor(log_fn):
    """Download SIGNOR human signaling data and filter phosphorylation events.

    Endpoint: https://signor.uniroma2.it/getData.php?organism=9606
    Format: TSV (no header row). Columns by position:
      0: ENTITYA (kinase)     1: TYPEA      2: IDA      3: DATABASEA
      4: ENTITYB (substrate)  5: TYPEB      6: IDB      7: DATABASEB
      8: EFFECT               9: MECHANISM  10: RESIDUE  11: SEQUENCE
      12: TAX_ID             13+: other fields
    Filter: MECHANISM == 'phosphorylation', both entities are proteins, RESIDUE non-empty
    """
    url = "https://signor.uniroma2.it/getData.php?organism=9606"
    log_fn("SIGNOR", f"Downloading from: {url}")

    try:
        response = _fetch_url(url, timeout=120)
        text = response.read().decode("utf-8", errors="replace")
    except Exception as e:
        log_fn("SIGNOR", f"DOWNLOAD FAILED: {e}")
        return []

    entries = []
    for line in text.strip().split("\n"):
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) < 13:
            continue

        mechanism = cols[9].strip().lower() if len(cols) > 9 else ""
        if mechanism != "phosphorylation":
            continue

        type_a = cols[1].strip() if len(cols) > 1 else ""
        type_b = cols[5].strip() if len(cols) > 5 else ""
        if type_a != "protein" or type_b != "protein":
            continue

        residue = cols[10].strip() if len(cols) > 10 else ""
        if not residue:
            continue

        kinase = cols[0].strip()
        substrate = cols[4].strip()
        peptide = cols[11].strip() if len(cols) > 11 else ""

        if kinase and substrate:
            entries.append({
                "kinase_gene": kinase,
                "substrate_gene": substrate,
                "phospho_site": residue,
                "heptameric_peptide": peptide,
            })

    log_fn("SIGNOR", f"Parsed {len(entries)} phosphorylation entries (protein-protein with site)")
    return entries


# ── Phase 2c: Download and parse UniProt ──────────────────────────────────────

def fetch_uniprot(log_fn):
    """Query UniProt REST API for human reviewed entries with kinase-attributed phospho sites.

    Endpoint: https://rest.uniprot.org/uniprotkb/search
    Query: reviewed human proteins with phosphoserine/phosphothreonine/phosphotyrosine
    Parse: Extract 'Modified residue' features where description contains 'by KINASE'

    The annotations look like: "Phosphoserine; by MTOR" or "Phosphotyrosine; by ABL1 and SRC"
    """
    base_url = "https://rest.uniprot.org/uniprotkb/search"
    query = "(organism_id:9606) AND (reviewed:true) AND (ft_mod_res:Phosphoserine OR ft_mod_res:Phosphothreonine OR ft_mod_res:Phosphotyrosine)"
    fields = "accession,gene_names,ft_mod_res"
    page_size = 500

    log_fn("UNIPROT", f"Querying UniProt REST API: {base_url}")
    log_fn("UNIPROT", f"  Query: {query}")
    log_fn("UNIPROT", f"  Fields: {fields}")

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

        url = f"{base_url}?{urllib.parse.urlencode(params)}"

        try:
            response = _fetch_url(url, timeout=120)
            # Extract next cursor from Link header for pagination
            link_header = response.headers.get("Link", "")
            next_cursor = None
            if link_header:
                for part in link_header.split(","):
                    if 'rel="next"' in part:
                        cursor_match = re.search(r'cursor=([^&>"]+)', part)
                        if cursor_match:
                            next_cursor = cursor_match.group(1)

            data = json.loads(response.read())
            results = data.get("results", [])
            if not results:
                break

            total_proteins += len(results)
            page_entries = _parse_uniprot_phospho(results)
            all_entries.extend(page_entries)

            page += 1
            if page % 5 == 0:
                log_fn("UNIPROT", f"  Page {page}: {total_proteins} proteins processed, "
                       f"{len(all_entries)} kinase-attributed entries so far")

            if not next_cursor:
                break
            cursor = next_cursor

        except Exception as e:
            log_fn("UNIPROT", f"  Page {page} FAILED: {e}")
            break

    log_fn("UNIPROT", f"Processed {total_proteins} proteins total")
    log_fn("UNIPROT", f"Parsed {len(all_entries)} kinase-attributed phosphorylation entries")
    return all_entries


def _parse_uniprot_phospho(proteins):
    """Extract kinase-attributed phospho entries from UniProt protein records."""
    entries = []

    # Map residue type from description
    residue_map = {
        "phosphoserine": "S",
        "phosphothreonine": "T",
        "phosphotyrosine": "Y",
        "phosphohistidine": "H",
    }

    for protein in proteins:
        accession = protein.get("primaryAccession", "")
        genes = protein.get("genes", [])
        substrate_gene = ""
        if genes:
            gene_name = genes[0].get("geneName", {})
            substrate_gene = gene_name.get("value", "") if isinstance(gene_name, dict) else ""

        if not substrate_gene:
            continue

        for feat in protein.get("features", []):
            if feat.get("type") != "Modified residue":
                continue

            desc = feat.get("description", "")
            desc_lower = desc.lower()

            # Must be a phosphorylation
            if "phospho" not in desc_lower:
                continue

            # Must have kinase attribution ("by KINASE")
            by_match = re.search(r"\bby\s+(\S+)", desc)
            if not by_match:
                continue

            # Extract all kinases (handles "by ABL1 and SRC" patterns)
            by_section = desc[by_match.start():]
            kinase_names = re.findall(r'\b([A-Z][A-Z0-9]{1,})\b', by_section)
            if not kinase_names:
                kinase_names = [by_match.group(1)]

            # Determine residue type
            residue = ""
            for key, code in residue_map.items():
                if key in desc_lower:
                    residue = code
                    break
            if not residue:
                continue

            # Get position
            location = feat.get("location", {})
            start = location.get("start", {})
            position = start.get("value", "") if isinstance(start, dict) else ""
            if not position:
                continue

            site = f"{residue}{position}"

            for kinase in kinase_names:
                # Filter out common non-kinase words that might match the pattern
                if kinase in ("AND", "OR", "THE", "IN", "VITRO", "VIVO", "NOT"):
                    continue
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


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_naive(log_fn):
    """Execute the naive (zero-shot) atlas curation pipeline.

    Phase 1: Discover databases (identify public API endpoints)
    Phase 2: Download and parse raw data from each database
    Phase 3: Merge, deduplicate, cross-reference, and QC
    """
    atlas, add_entry = _make_atlas_dict()

    # ── Phase 1: Discover databases ──────────────────────────────────────
    log_fn("PHASE1", "=" * 60)
    log_fn("PHASE1", "PHASE 1: DISCOVER DATABASES")
    log_fn("PHASE1", "=" * 60)

    log_fn("DISCOVER", "Identifying public databases for human kinase-substrate-phosphosite data...")
    log_fn("DISCOVER", "")
    log_fn("DISCOVER", "Database 1: PhosphoSitePlus (PSP)")
    log_fn("DISCOVER", "  URL: https://www.phosphosite.org/downloads/Kinase_Substrate_Dataset.gz")
    log_fn("DISCOVER", "  Description: Curated kinase-substrate relationships with phosphorylation")
    log_fn("DISCOVER", "  sites, heptameric peptides, and in vivo/in vitro evidence.")
    log_fn("DISCOVER", "  Format: Gzipped TSV. Filter by SUB_ORGANISM='human'.")
    log_fn("DISCOVER", "")
    log_fn("DISCOVER", "Database 2: SIGNOR")
    log_fn("DISCOVER", "  URL: https://signor.uniroma2.it/getData.php?organism=9606")
    log_fn("DISCOVER", "  Description: Curated signaling network with phosphorylation events,")
    log_fn("DISCOVER", "  mechanisms, residue sites, and PubMed references.")
    log_fn("DISCOVER", "  Format: TSV (no header). Filter by MECHANISM='phosphorylation'.")
    log_fn("DISCOVER", "")
    log_fn("DISCOVER", "Database 3: UniProt/UniProtKB")
    log_fn("DISCOVER", "  URL: https://rest.uniprot.org/uniprotkb/search")
    log_fn("DISCOVER", "  Query: reviewed human proteins with phospho modifications")
    log_fn("DISCOVER", "  Description: Swiss-Prot reviewed entries with kinase-attributed")
    log_fn("DISCOVER", "  phosphorylation sites in 'Modified residue' feature annotations.")
    log_fn("DISCOVER", "  Parse pattern: 'Phosphoserine; by KINASE_NAME'")

    # ── Phase 2: Download and parse each database ────────────────────────
    log_fn("PHASE2", "=" * 60)
    log_fn("PHASE2", "PHASE 2: DOWNLOAD AND PARSE DATABASE DATA")
    log_fn("PHASE2", "=" * 60)

    # PSP
    log_fn("PHASE2", "--- PhosphoSitePlus ---")
    psp_entries = fetch_psp(log_fn)
    for e in psp_entries:
        add_entry(
            e["kinase_gene"], e["substrate_gene"], e["phospho_site"],
            e.get("substrate_uniprot", ""), e.get("heptameric_peptide", ""),
            "PhosphoSitePlus",
        )
    log_fn("PHASE2", f"  Atlas after PSP: {len(atlas)} unique triplets")

    # SIGNOR
    log_fn("PHASE2", "--- SIGNOR ---")
    signor_entries = fetch_signor(log_fn)
    for e in signor_entries:
        add_entry(
            e["kinase_gene"], e["substrate_gene"], e["phospho_site"],
            "", e.get("heptameric_peptide", ""),
            "SIGNOR",
        )
    log_fn("PHASE2", f"  Atlas after SIGNOR: {len(atlas)} unique triplets")

    # UniProt
    log_fn("PHASE2", "--- UniProt ---")
    uniprot_entries = fetch_uniprot(log_fn)
    for e in uniprot_entries:
        add_entry(
            e["kinase_gene"], e["substrate_gene"], e["phospho_site"],
            e.get("substrate_uniprot", ""), "",
            "UniProt",
        )
    log_fn("PHASE2", f"  Atlas after UniProt: {len(atlas)} unique triplets")

    # ── Phase 3: Cross-reference, merge, QC ──────────────────────────────
    log_fn("PHASE3", "=" * 60)
    log_fn("PHASE3", "PHASE 3: CROSS-REFERENCE AND QUALITY CONTROL")
    log_fn("PHASE3", "=" * 60)

    # Cross-reference stats
    multi_db = sum(1 for e in atlas.values() if len(e["supporting_databases"]) >= 2)
    triple_db = sum(1 for e in atlas.values() if len(e["supporting_databases"]) >= 3)
    log_fn("XREF", f"  Entries in 2+ databases: {multi_db}")
    log_fn("XREF", f"  Entries in all 3 databases: {triple_db}")

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

    # ── Final assembly ───────────────────────────────────────────────────
    log_fn("RESULT", "=== FINAL ATLAS ===")
    return _finalize(atlas, log_fn)


# ── Main entry point ──────────────────────────────────────────────────────────

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

    # Load naive prompt for reference
    prompt_path = Path("agents/prompts/naive.txt")
    prompt = prompt_path.read_text() if prompt_path.exists() else "(naive prompt not found)"
    log_fn("SETUP", f"Prompt: {prompt_path} ({len(prompt)} chars)")
    log_fn("SETUP", "Agent: Claude Sonnet 4.6")
    log_fn("SETUP", "Condition: naive (zero-shot)")
    log_fn("SETUP", f"Output: {out_dir}")
    log_fn("SETUP", "Strategy: Independently discover database APIs, download raw data,")
    log_fn("SETUP", "  parse kinase-substrate-site triplets, merge with dedup, cross-reference.")

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

    # Compute per-DB counts for run_log
    db_counts = {}
    multi_db_count = 0
    for e in entries:
        for db in e["supporting_databases"]:
            db_counts[db] = db_counts.get(db, 0) + 1
        if len(e["supporting_databases"]) >= 2:
            multi_db_count += 1

    kinase_set = set(e["kinase_gene"] for e in entries)

    # Build strategy summary
    strategy_summary = (
        "Naive (zero-shot) pipeline: Independently discovered 3 database APIs. "
        "(1) PSP: Downloaded Kinase_Substrate_Dataset.gz from phosphosite.org, "
        "parsed gzipped TSV, filtered for human SUB_ORGANISM. "
        "(2) SIGNOR: Queried getData.php?organism=9606, parsed TSV, filtered for "
        "mechanism=phosphorylation with protein-protein entries and non-empty residue. "
        "(3) UniProt: Queried rest.uniprot.org REST API for reviewed human proteins "
        "with phospho modifications, parsed 'Modified residue' features with kinase "
        "attribution ('by KINASE' pattern). "
        "Merged all entries by (kinase|substrate|site) key, aggregating supporting "
        "databases, peptides, and UniProt IDs. "
        f"Final atlas: {len(entries)} unique triplets, {len(kinase_set)} kinases, "
        f"{multi_db_count} multi-DB entries ({multi_db_count / max(len(entries), 1) * 100:.1f}%)."
    )

    # Save run_log.json
    run_log = {
        "agent": "Claude Sonnet 4.6",
        "prompt": "naive (zero-shot)",
        "strategy": strategy_summary,
        "databases_accessed": sorted(db_counts.keys()),
        "api_endpoints": {
            "PhosphoSitePlus": "https://www.phosphosite.org/downloads/Kinase_Substrate_Dataset.gz",
            "SIGNOR": "https://signor.uniroma2.it/getData.php?organism=9606",
            "UniProt": "https://rest.uniprot.org/uniprotkb/search?query=(organism_id:9606)+AND+(reviewed:true)+AND+(ft_mod_res:Phospho*)",
        },
        "raw_counts": {
            "PSP": db_counts.get("PhosphoSitePlus", 0),
            "SIGNOR": db_counts.get("SIGNOR", 0),
            "UniProt": db_counts.get("UniProt", 0),
        },
        "merged_atlas": len(entries),
        "unique_kinases": len(kinase_set),
        "unique_substrates": len(set(e["substrate_gene"] for e in entries)),
        "multi_db_entries": multi_db_count,
    }
    with open(out_dir / "run_log.json", "w") as f:
        json.dump(run_log, f, indent=2)

    # Save detailed log
    with open(out_dir / "run.log", "w") as f:
        f.write("\n".join(log_lines))

    # Run scorer
    log_fn("SCORE", "Running evaluation scorer...")
    gold_path = "gold_standard/parsed/phosphoatlas_gold.json"
    if Path(gold_path).exists():
        from evaluation.scorer import load_gold, score_atlas, score_per_kinase
        gold = load_gold(gold_path)
        scores = score_atlas(entries, gold)

        # Save summary.json
        cl = scores["column_level"]
        summary = {k: v for k, v in scores.items()}
        summary["column_level"] = {k: v for k, v in cl.items() if k != "peptide_mismatches"}
        with open(scores_dir / "summary.json", "w") as f:
            json.dump(summary, f, indent=2)

        # Save per_kinase.json
        per_kinase = score_per_kinase(entries, gold)
        with open(scores_dir / "per_kinase.json", "w") as f:
            json.dump(per_kinase, f, indent=2)

        # Save peptide_mismatches.json
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

        # Check thresholds
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

    # Re-save log with scoring output
    with open(out_dir / "run.log", "w") as f:
        f.write("\n".join(log_lines))

    print(f"\n{'=' * 60}")
    print(f"Outputs in: {out_dir}/")
    print(f"  atlas.json, run_log.json, run.log")
    print(f"  scores/summary.json, scores/per_kinase.json, scores/peptide_mismatches.json")


if __name__ == "__main__":
    main()
