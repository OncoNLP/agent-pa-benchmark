#!/usr/bin/env python3
"""
Build a best-effort live human phosphorylation atlas JSON from online-accessible sources.

Current implementation:
- Harvests phosphorylation relationships from PhosphoSIGNOR live TSV API
- Reconstructs kinase -> substrate -> phosphosite triplets from paired phosphosite-node rows
- Emits a JSON atlas with fields compatible with the benchmark README

Output schema per entry:
{
  "kinase_gene": "ABL1",
  "substrate_gene": "ABI1",
  "phospho_site": "Y213",
  "heptameric_peptide": null,
  "substrate_uniprot": "Q8IZP0",
  "supporting_databases": ["PhosphoSIGNOR"]
}

Notes:
- This is best-effort, not exhaustive.
- PhosphoSitePlus bulk download was license/login gated in the execution environment.
- UniProt integration was not completed in this pass.
"""

from __future__ import annotations

import csv
import json
import re
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

PHOSPHOSIGNOR_URL = (
    "https://signor.uniroma2.it/PhosphoSIGNOR/apis/v1/index.php"
    "?role=kinaseALL&format=tsv&header=yes"
)


def fetch_text(url: str, user_agent: str = "Mozilla/5.0") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", "replace")


def three_letter_site_to_one_letter(site: str) -> str:
    """Convert e.g. Ser383 -> S383."""
    m = re.match(r"([A-Za-z]{3})(\d+)$", site)
    if not m:
        return site
    aa3, pos = m.groups()
    aa_map = {
        "Ser": "S",
        "Thr": "T",
        "Tyr": "Y",
        "His": "H",
        "Lys": "K",
        "Asp": "D",
        "Glu": "E",
        "Cys": "C",
        "Arg": "R",
        "Asn": "N",
        "Gln": "Q",
        "Gly": "G",
        "Pro": "P",
        "Ala": "A",
        "Val": "V",
        "Ile": "I",
        "Leu": "L",
        "Met": "M",
        "Phe": "F",
        "Trp": "W",
    }
    return f"{aa_map.get(aa3.capitalize(), aa3)}{pos}"


def parse_phosphosignor_tsv(tsv_text: str) -> list[dict]:
    """
    Parse PhosphoSIGNOR kinaseALL TSV.

    Data pattern:
      kinase_protein -> substrate_phosphosite_node
      substrate_phosphosite_node -> substrate_protein

    We reconstruct the triplet by pairing rows with the same SIGNOR phosphosite node.
    """
    rows = list(csv.DictReader(tsv_text.splitlines(), delimiter="\t"))
    by_signor_id: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_signor_id[row["signor_id"]].append(row)

    site_re = re.compile(r"^(?P<acc>[^_]+)_ph(?P<site>[A-Za-z]{3}\d+)$")
    entries: dict[tuple[str, str, str], dict] = {}

    for signor_id, group in by_signor_id.items():
        kinase_to_site = []
        site_to_substrate = []

        for row in group:
            mechanism = row.get("mechanism", "")
            entity_a = row.get("entityA", "")
            entity_b = row.get("entityB", "")

            if mechanism != "phosphorylation":
                continue

            # kinase accession -> phosphosite node accession
            if "_ph" not in entity_a and "_ph" in entity_b:
                kinase_to_site.append(row)
            # phosphosite node accession -> substrate accession
            elif "_ph" in entity_a and "_ph" not in entity_b:
                site_to_substrate.append(row)

        for krow in kinase_to_site:
            phosphosite_node = krow["entityB"]
            m = site_re.match(phosphosite_node)
            if not m:
                continue

            substrate_acc = m.group("acc")
            phospho_site = three_letter_site_to_one_letter(m.group("site"))
            kinase_gene = krow.get("entityA_name", "").strip()

            for srow in site_to_substrate:
                if srow.get("entityA") != phosphosite_node:
                    continue
                if srow.get("entityB") != substrate_acc:
                    continue

                substrate_gene = srow.get("entityB_name", "").strip()
                if not kinase_gene or not substrate_gene or not phospho_site:
                    continue

                key = (kinase_gene, substrate_gene, phospho_site)
                if key not in entries:
                    entries[key] = {
                        "kinase_gene": kinase_gene,
                        "substrate_gene": substrate_gene,
                        "phospho_site": phospho_site,
                        "heptameric_peptide": None,
                        "substrate_uniprot": substrate_acc,
                        "supporting_databases": ["PhosphoSIGNOR"],
                    }
                else:
                    support = entries[key].setdefault("supporting_databases", [])
                    if "PhosphoSIGNOR" not in support:
                        support.append("PhosphoSIGNOR")
                    if not entries[key].get("substrate_uniprot"):
                        entries[key]["substrate_uniprot"] = substrate_acc

    return sorted(
        entries.values(),
        key=lambda x: (x["kinase_gene"], x["substrate_gene"], x["phospho_site"]),
    )


def main(argv: list[str]) -> int:
    out_path = Path(argv[1]) if len(argv) > 1 else Path("atlas_from_phosphosignor.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Fetching PhosphoSIGNOR TSV from: {PHOSPHOSIGNOR_URL}")
    tsv_text = fetch_text(PHOSPHOSIGNOR_URL)

    print("Parsing and reconstructing kinase-substrate-site triplets...")
    atlas = parse_phosphosignor_tsv(tsv_text)

    out_path.write_text(json.dumps(atlas, indent=2))

    kinase_count = len({x["kinase_gene"] for x in atlas})
    substrate_count = len({x["substrate_gene"] for x in atlas})

    print(f"Wrote atlas to: {out_path}")
    print(f"Entries: {len(atlas)}")
    print(f"Unique kinases: {kinase_count}")
    print(f"Unique substrates: {substrate_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
