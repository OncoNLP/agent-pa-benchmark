"""
Normalize agent outputs for comparison against gold standard.
Handles gene symbol aliases, UniProt ID formats, phospho-site formats.

This is the SINGLE SOURCE OF TRUTH for normalization across all steps.
"""
import re
from typing import Optional


# Common gene symbol aliases -> canonical HGNC symbols
GENE_ALIASES: dict[str, str] = {
    "CDC2": "CDK1", "p34cdc2": "CDK1", "PCTAIRE1": "CDK16",
    "c-Src": "SRC", "pp60c-src": "SRC",
    "c-Abl": "ABL1", "Abl": "ABL1", "ABL": "ABL1",
    "PKCalpha": "PRKCA", "PKCa": "PRKCA",
    "PKA": "PRKACA", "PKACa": "PRKACA",
    "CK2": "CSNK2A1", "CK2alpha": "CSNK2A1", "CK2a": "CSNK2A1",
    "ERK1": "MAPK3", "ERK2": "MAPK1",
    "p38": "MAPK14", "p38alpha": "MAPK14",
    "JNK1": "MAPK8", "JNK2": "MAPK9",
    "GSK3beta": "GSK3B", "GSK-3beta": "GSK3B",
    "Akt1": "AKT1", "PKB": "AKT1", "PKBa": "AKT1",
    "mTOR": "MTOR", "FRAP": "MTOR", "FRAP1": "MTOR",
    "Chk1": "CHEK1", "Chk2": "CHEK2",
    "AMPK": "PRKAA1", "BARK": "ADRBK1", "BARK1": "ADRBK1",
}


def normalize_gene_symbol(gene: str) -> str:
    """Normalize a gene symbol to its canonical HGNC form."""
    if not gene:
        return ""
    gene = gene.strip()
    return GENE_ALIASES.get(gene, gene.upper())


def normalize_uniprot_id(uid: Optional[str]) -> Optional[str]:
    """Normalize a UniProt accession ID."""
    if not uid:
        return None
    uid = uid.strip()
    uid = re.sub(r"^(sp|tr)\|", "", uid)
    uid = uid.split("|")[0]
    return uid


def normalize_phospho_site(site: str) -> str:
    """Normalize phospho-site format to uppercase residue + position (e.g., Y393)."""
    if not site:
        return ""
    site = site.strip()
    three_to_one = {
        "Tyr": "Y", "TYR": "Y", "Ser": "S", "SER": "S",
        "Thr": "T", "THR": "T", "His": "H", "HIS": "H",
    }
    site = re.sub(r"^p", "", site)
    for three, one in three_to_one.items():
        if site.startswith(three):
            site = one + site[len(three):]
            break
    site = re.sub(r"([A-Za-z])[\s\-]+(\d)", r"\1\2", site)
    if site and site[0].isalpha():
        site = site[0].upper() + site[1:]
    return site


def make_triplet_key(kinase_gene: str, substrate_gene: str, phospho_site: str) -> str:
    """Create a normalized triplet key for matching."""
    k = normalize_gene_symbol(kinase_gene)
    s = normalize_gene_symbol(substrate_gene)
    p = normalize_phospho_site(phospho_site)
    return f"{k}|{s}|{p}"
