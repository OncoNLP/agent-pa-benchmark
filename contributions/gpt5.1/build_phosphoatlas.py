#!/usr/bin/env python3
import json
from pathlib import Path
import openpyxl

BASE = Path(__file__).resolve().parent
INPUT_JSON = BASE / "phosphoatlas_combined.json"
OUTPUT_XLSX = BASE / "phosphoatlas_kinase_substrate_sites.xlsx"
OUTPUT_JSON = BASE / "phosphoatlas_kinase_substrate_sites.json"

# Header layout based on gold_standard/sample_PA2.xlsx
HEADERS = [
    "KINASE GENE",
    "KINASE common name",
    "KIN_ACC_ID",
    "SUBSTRATE common name",
    "SUB_GENE_ID",
    "SUB_ACC_ID",
    "SUBSTRATE GENE",
    "SUB_MOD_RSD",
    "SITE_GRP_ID",
    "SITE_+/-7_AA",
    "BE AWARE: available in PA2_2023, or in PA1_2016_HTKAM2_only",
    "",  # trailing empty column in sample
]

# Map from input JSON keys to output headers
KEY_MAP = {
    "KINASE GENE": "KINASE GENE",
    "KINASE common name": "KINASE common name",
    "KIN_ACC_ID": "KIN_ACC_ID",
    "SUBSTRATE common name": "SUBSTRATE common name",
    "SUB_GENE_ID": "SUB_GENE_ID",
    "SUB_ACC_ID": "SUB_ACC_ID",
    "SUBSTRATE GENE": "SUBSTRATE GENE",
    "SUB_MOD_RSD": "SUB_MOD_RSD",
    "SITE_GRP_ID": "SITE_GRP_ID",
    "SITE_+/-7_AA": "SITE_+/-7_AA",
    # Input uses "BE AWARE"; output requires full header text
    "BE AWARE": "BE AWARE: available in PA2_2023, or in PA1_2016_HTKAM2_only",
}


def main():
    with INPUT_JSON.open() as f:
        records = json.load(f)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "phosphoatlas"

    # Write header
    for col, header in enumerate(HEADERS, start=1):
        ws.cell(row=1, column=col, value=header)

    # Write rows
    for r_idx, rec in enumerate(records, start=2):
        # Build row by header order
        row = {h: None for h in HEADERS}
        for k, v in rec.items():
            if k in KEY_MAP:
                row[KEY_MAP[k]] = v
        for c_idx, header in enumerate(HEADERS, start=1):
            ws.cell(row=r_idx, column=c_idx, value=row.get(header))

    wb.save(OUTPUT_XLSX)

    # Also write JSON output (array of records with schema keys)
    out_records = []
    for rec in records:
        out = {h: None for h in HEADERS}
        for k, v in rec.items():
            if k in KEY_MAP:
                out[KEY_MAP[k]] = v
        # Use empty-string key for the trailing blank column, matching sample header
        if "" in out:
            out[""] = ""
        out_records.append(out)

    with OUTPUT_JSON.open("w") as f:
        json.dump(out_records, f, ensure_ascii=False)

    print(f"Wrote {OUTPUT_XLSX} and {OUTPUT_JSON} with {len(records)} rows")


if __name__ == "__main__":
    main()
