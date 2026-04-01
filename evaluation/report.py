#!/usr/bin/env python3
"""
Generate a PDF comparison report across all model contributions.

Scans contributions/ for summary.json files, builds comparison tables,
and outputs a PDF to paper/tables/.

Usage:
    python -m evaluation.report
    python -m evaluation.report --output paper/tables/my_report.pdf
"""
import argparse
import json
from pathlib import Path

from fpdf import FPDF


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONTRIBUTIONS = PROJECT_ROOT / "contributions"
DEFAULT_OUTPUT = PROJECT_ROOT / "paper" / "tables" / "benchmark_summary_tables.pdf"


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

# Map contribution folder names to display labels and metadata
MODEL_META = {
    "claude_opus_naive": ("Claude Opus 4.6", "Proprietary", "naive"),
    "claude_opus_paper_informed": ("Claude Opus 4.6", "Proprietary", "paper_informed"),
    "claude_opus_pipeline_guided": ("Claude Opus 4.6", "Proprietary", "pipeline_guided"),
    "claude_sonnet_naive": ("Claude Sonnet 4.6", "Proprietary", "naive"),
    "gemini_3.1_pro_naive": ("Gemini 3.1 Pro", "Proprietary", "naive"),
    "gemini_3.1_pro_naive/gemini-3.1-pro": ("Gemini 3.1 Pro", "Proprietary", "naive"),
    "gemini_3.1_pro_naive/gemini-2.5-flash": ("Gemini 2.5 Flash", "Proprietary", "naive"),
    "gemini_3.1_pro_naive/gemini-3.0-flash": ("Gemini 3.0 Flash", "Proprietary", "naive"),
    "gemini_3.1_pro_paper_informed": ("Gemini 3.1 Pro", "Proprietary", "paper_informed"),
    "gemini_3.1_pro_paper_informed/gemini-3.1-pro": ("Gemini 3.1 Pro", "Proprietary", "paper_informed"),
    "gemini_3.1_pro_pipeline_guided": ("Gemini 3.1 Pro", "Proprietary", "pipeline_guided"),
    "gemini_3.1_pro_pipeline_guided/gemini-3.1-pro": ("Gemini 3.1 Pro", "Proprietary", "pipeline_guided"),
    "mistral_large_naive": ("Mistral Large", "Proprietary", "naive"),
    "mistral_large_paper_informed": ("Mistral Large", "Proprietary", "paper_informed"),
    "openclaw_live_best_effort": ("OpenCLAW Live", "Open-source", "naive"),
    "andrew_qwen3_235b": ("Qwen 3.2 35B", "Open-source", "naive"),
    "andrew_qwen3_235b/qwen_prompt_testing": ("Qwen 3.2 35B", "Open-source", "naive"),
    "andrew_qwen3_235b/paper_informed": ("Qwen 3.2 35B", "Open-source", "paper_informed"),
    "andrew_qwen3_235b/explicit_prompt": ("Qwen 3.2 35B", "Open-source", "explicit_prompt"),
    "andrew_qwen3_235b/pipeline_informed": ("Qwen 3.2 35B", "Open-source", "pipeline_informed"),
}

# Folders to skip (duplicates or partial runs)
SKIP_FOLDERS = {
    "mistral_large",           # duplicate of mistral_large_naive
    "claude_opus_iterative",   # iterative experiment, not primary
}



def collect_results():
    """Walk contributions/ and load all summary.json files."""
    results = []
    for folder in sorted(CONTRIBUTIONS.iterdir()):
        if not folder.is_dir() or folder.name == "example" or folder.name in SKIP_FOLDERS:
            continue
        # Direct scores/summary.json
        summary = folder / "scores" / "summary.json"
        if summary.exists():
            results.append(_load_one(folder.name, summary))
        # Nested: scores/<submodel>/summary.json (e.g. gemini variants)
        # Only use the primary model (e.g. gemini-3.1-pro), skip flash variants
        scores_dir = folder / "scores"
        if scores_dir.is_dir():
            for sub in sorted(scores_dir.iterdir()):
                s = sub / "summary.json" if sub.is_dir() else None
                if s and s.exists():
                    results.append(_load_one(f"{folder.name}/{sub.name}", s))
        # Nested results dirs (e.g. qwen)
        if (folder / "results").is_dir():
            for res_sub in sorted((folder / "results").glob("*/scores/summary.json")):
                cond = res_sub.parent.parent.name
                results.append(_load_one(f"{folder.name}/{cond}", res_sub))
        # qwen_prompt_testing (use as Qwen naive if main atlas is partial)
        qpt = folder / "qwen_prompt_testing" / "scores" / "summary.json"
        if qpt.exists():
            results.append(_load_one(f"{folder.name}/qwen_prompt_testing", qpt))
    # Deduplicate: if same (model, condition) appears multiple times, keep largest atlas
    seen = {}
    deduped = []
    for r in results:
        key = (r["model"], r["condition"])
        if key in seen:
            # Keep the one with more entries (larger = more complete run)
            if r["entries"] > seen[key]["entries"]:
                deduped = [x for x in deduped if (x["model"], x["condition"]) != key]
                seen[key] = r
                deduped.append(r)
        else:
            seen[key] = r
            deduped.append(r)
    return deduped


def _load_one(label, path):
    with open(path) as f:
        d = json.load(f)
    ov = d.get("overview", {})
    al = d.get("atlas_level", {})
    cl = d.get("column_level", {})
    kd = d.get("kinase_discovery", {})
    cr = d.get("cross_referencing", {})
    pt = d.get("per_tier", {})

    # Handle legacy field names (scores generated before case-insensitive update)
    # Old scorer: peptide_close_accuracy (case-insensitive)
    # New scorer: peptide_accuracy (case-insensitive, primary)
    if "peptide_accuracy" not in cl and "peptide_close_accuracy" in cl:
        cl["peptide_accuracy"] = cl["peptide_close_accuracy"]
    if "peptide_mismatch_rate" not in cl and "peptide_mismatch" in cl and cl.get("peptide_total", 0) > 0:
        cl["peptide_mismatch_rate"] = round(cl["peptide_mismatch"] / cl["peptide_total"], 4)
    if "peptide_missing_count" not in cl:
        cl["peptide_missing_count"] = cl.get("peptide_missing", 0)

    # Resolve display name
    meta = MODEL_META.get(label)
    if meta:
        model, mtype, condition = meta
    else:
        parts = label.split("/")
        base_meta = MODEL_META.get(parts[0]) if parts else None
        model = base_meta[0] if base_meta else parts[0]
        mtype = base_meta[1] if base_meta else "Unknown"
        condition = parts[1] if len(parts) > 1 else "naive"

    # Load run_log if available for tool calls
    run_log_path = path.parent.parent / "run_log.json"
    tool_calls = "?"
    runtime = "?"
    if run_log_path.exists():
        rl = json.load(open(run_log_path))
        tc = rl.get("tool_calls", rl.get("total_tool_calls", rl.get("db_tool_calls")))
        if isinstance(tc, dict):
            tool_calls = str(tc.get("total", "?"))
        elif tc is not None:
            tool_calls = str(tc)
        elapsed = rl.get("elapsed_seconds", rl.get("elapsed"))
        if isinstance(elapsed, (int, float)):
            runtime = f"{elapsed/60:.1f}m" if elapsed > 300 else f"{elapsed:.0f}s"
        dbs_list = rl.get("databases_accessed", rl.get("databases", []))
        if isinstance(dbs_list, list):
            dbs_str = ", ".join(dbs_list)
        elif isinstance(dbs_list, dict):
            dbs_str = ", ".join(dbs_list.keys())
        else:
            dbs_str = str(dbs_list)
    else:
        dbs_str = ", ".join(cr.get("db_coverage", {}).keys())

    return {
        "label": label,
        "model": model,
        "type": mtype,
        "condition": condition,
        "entries": al.get("total_agent_entries", ov.get("atlas_size", 0)),
        "recall": al.get("recall", ov.get("recall", 0)),
        "precision": al.get("precision", ov.get("precision", 0)),
        "f1": al.get("f1", ov.get("f1", 0)),
        "tp": al.get("true_positives", 0),
        "fp": al.get("false_positives", 0),
        "fn": al.get("false_negatives", 0),
        "kinases_found": kd.get("kinases_discovered", 0),
        "kinases_total": kd.get("kinases_in_gold", 433),
        "kinase_rate": kd.get("discovery_rate", 0),
        "multi_db_pct": cr.get("multi_db_pct", 0),
        "peptide_accuracy": cl.get("peptide_accuracy", 0),
        "peptide_exact_accuracy": cl.get("peptide_exact_accuracy", 0),
        "peptide_mismatch": cl.get("peptide_mismatch", 0),
        "peptide_missing": cl.get("peptide_missing_count", cl.get("peptide_missing", 0)),
        "peptide_total": cl.get("peptide_total", 0),
        "uniprot_accuracy": cl.get("uniprot_accuracy", 0),
        "tier_a": pt.get("A", {}).get("recall", 0),
        "tier_b": pt.get("B", {}).get("recall", 0),
        "tier_c": pt.get("C", {}).get("recall", 0),
        "tier_d": pt.get("D", {}).get("recall", 0),
        "tool_calls": tool_calls,
        "runtime": runtime,
        "dbs": dbs_str,
    }


# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------

class Report(FPDF):
    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")

    def table_caption(self, num, text):
        self.set_font("Helvetica", "B", 10)
        self.cell(0, 7, f"Table {num}. {text}", new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def tbl(self, headers, rows, col_widths=None):
        if not col_widths:
            col_widths = [190 / len(headers)] * len(headers)
        self.set_font("Helvetica", "B", 8)
        self.set_fill_color(50, 80, 120)
        self.set_text_color(255, 255, 255)
        for i, h in enumerate(headers):
            self.cell(col_widths[i], 6, h, 1, 0, "C", fill=True)
        self.ln()
        self.set_text_color(0, 0, 0)
        self.set_font("Helvetica", "", 7.5)
        for j, row in enumerate(rows):
            self.set_fill_color(240, 240, 245) if j % 2 == 0 else self.set_fill_color(255, 255, 255)
            for i, c in enumerate(row):
                self.cell(col_widths[i], 5.5, str(c), 1, 0, "C", fill=True)
            self.ln()
        self.ln(4)


def _fmt(v, pct=False):
    if isinstance(v, float):
        return f"{v:.4f}" if not pct else f"{v*100:.1f}%"
    return str(v)


def generate_report(output_path):
    results = collect_results()
    if not results:
        print("No results found in contributions/")
        return

    # Separate naive vs other conditions
    naive = [r for r in results if r["condition"] == "naive"]
    naive.sort(key=lambda r: r["f1"], reverse=True)

    pdf = Report()
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(True, 20)

    # --- Title ---
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.ln(30)
    pdf.cell(0, 12, "PhosphoAtlas Agent Benchmark", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, "Summary Tables", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "I", 9)
    pdf.cell(0, 6, "Peptide accuracy = case-insensitive (biological identity match)", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(10)

    # === Table 1: Overall naive ===
    pdf.add_page()
    pdf.table_caption(1, "Overall performance comparison across models (naive prompt condition).")

    t1_rows = []
    for r in naive:
        t1_rows.append([
            r["model"], r["type"], f"{r['entries']:,}",
            _fmt(r["recall"]), _fmt(r["precision"]), _fmt(r["f1"]),
            f"{r['kinases_found']}/{r['kinases_total']}",
            _fmt(r["peptide_accuracy"], pct=True),
            f"{r['multi_db_pct']}%",
            r["dbs"][:30] or "N/A",
        ])
    pdf.tbl(
        ["Model", "Type", "Entries", "Recall", "Prec", "F1", "Kinases", "Pep Acc", "Multi-DB", "DBs Used"],
        t1_rows,
        col_widths=[24, 16, 14, 12, 12, 10, 16, 14, 12, 60],
    )

    # === Table 2: Prompt engineering effect ===
    pdf.table_caption(2, "Effect of prompt engineering on model performance.")
    # Group by model
    by_model = {}
    for r in results:
        by_model.setdefault(r["model"], []).append(r)

    t2_rows = []
    for model in ["Claude Opus 4.6", "Gemini 3.1 Pro", "Mistral Large", "Qwen 3.2 35B"]:
        runs = by_model.get(model, [])
        naive_run = next((r for r in runs if r["condition"] == "naive"), None)
        for r in sorted(runs, key=lambda x: x["condition"]):
            if naive_run and r["condition"] != "naive":
                delta_r = r["recall"] - naive_run["recall"]
                delta_str = f"{delta_r:+.3f} R" if abs(delta_r) > 0.001 else "No change"
            else:
                delta_str = "--"
            t2_rows.append([
                r["model"], r["condition"],
                _fmt(r["recall"]), _fmt(r["f1"]),
                _fmt(r["peptide_accuracy"], pct=True),
                f"{r['multi_db_pct']}%",
                delta_str,
            ])
    pdf.tbl(
        ["Model", "Condition", "Recall", "F1", "Pep Acc", "Multi-DB", "vs Naive"],
        t2_rows,
        col_widths=[24, 22, 14, 12, 14, 14, 90],
    )

    # === Table 3: Opus vs Sonnet adjusted ===
    pdf.add_page()
    opus_n = next((r for r in results if r["label"] == "claude_opus_naive"), None)
    sonnet_n = next((r for r in results if r["label"] == "claude_sonnet_naive"), None)
    if opus_n and sonnet_n:
        pdf.table_caption(3, "Claude Opus vs Sonnet adjusted comparison (assuming FPs are valid novel entries).")
        pdf.tbl(
            ["Metric", "Opus", "Sonnet", "Winner"],
            [
                ["Gold entries found (TP)", f"{opus_n['tp']:,}", f"{sonnet_n['tp']:,}",
                 f"Opus (+{opus_n['tp']-sonnet_n['tp']:,})"],
                ["Novel entries (likely valid)", f"{opus_n['fp']:,}", f"{sonnet_n['fp']:,}",
                 f"Opus (+{opus_n['fp']-sonnet_n['fp']:,})"],
                ["Total real data curated", f"{opus_n['tp']+opus_n['fp']:,}", f"{sonnet_n['tp']+sonnet_n['fp']:,}",
                 f"Opus (+{(opus_n['tp']+opus_n['fp'])-(sonnet_n['tp']+sonnet_n['fp']):,})"],
                ["Kinases found", f"{opus_n['kinases_found']}/{opus_n['kinases_total']}",
                 f"{sonnet_n['kinases_found']}/{sonnet_n['kinases_total']}", "Opus"],
                ["Multi-DB cross-validation", f"{opus_n['multi_db_pct']}%", f"{sonnet_n['multi_db_pct']}%", "Opus"],
                ["Peptide accuracy (case-insensitive)", _fmt(opus_n["peptide_accuracy"], pct=True),
                 _fmt(sonnet_n["peptide_accuracy"], pct=True), "Comparable"],
                ["Peptide true mismatches",
                 f"{opus_n['peptide_mismatch']}/{opus_n['peptide_total']}",
                 f"{sonnet_n['peptide_mismatch']}/{sonnet_n['peptide_total']}", "Sonnet"],
                ["UniProt accuracy", _fmt(opus_n["uniprot_accuracy"], pct=True),
                 _fmt(sonnet_n["uniprot_accuracy"], pct=True), "Comparable"],
            ],
            col_widths=[42, 30, 30, 88],
        )

    # === Table 4: Tool call efficiency ===
    pdf.table_caption(4, "Tool call efficiency across models and conditions.")
    t4_rows = []
    for r in results:
        if r["label"].startswith("andrew_qwen") or r["label"].startswith("claude_opus_iterative"):
            continue
        t4_rows.append([
            r["model"], r["condition"], r["tool_calls"], r["runtime"],
            r["dbs"][:30] or "N/A", f"{r['entries']:,}",
        ])
    pdf.tbl(
        ["Model", "Condition", "Tool Calls", "Runtime", "DBs Queried", "Entries"],
        t4_rows,
        col_widths=[24, 20, 18, 14, 50, 64],
    )

    # === Table 5: Per-tier recall ===
    pdf.add_page()
    pdf.table_caption(5, "Per-tier recall by kinase size (naive prompt condition).")
    t5_rows = []
    for r in naive:
        t5_rows.append([
            r["model"],
            _fmt(r["tier_a"]) if r["tier_a"] else "--",
            _fmt(r["tier_b"]) if r["tier_b"] else "--",
            _fmt(r["tier_c"]) if r["tier_c"] else "--",
            _fmt(r["tier_d"]) if r["tier_d"] else "--",
        ])
    pdf.tbl(
        ["Model", "Tier A (100+)", "Tier B (20-99)", "Tier C (5-19)", "Tier D (<5)"],
        t5_rows,
        col_widths=[40, 37, 38, 37, 38],
    )

    # === Table 6: Column-level accuracy ===
    pdf.table_caption(6, "Column-level accuracy for matched entries (naive prompt condition).")
    t6_rows = []
    for r in naive:
        t6_rows.append([
            r["model"], f"{r['tp']:,}",
            _fmt(r["peptide_accuracy"], pct=True),
            _fmt(r["peptide_exact_accuracy"], pct=True),
            str(r["peptide_mismatch"]),
            str(r["peptide_missing"]),
            _fmt(r["uniprot_accuracy"], pct=True),
        ])
    pdf.tbl(
        ["Model", "Matched", "Pep Acc", "Pep Exact", "Pep Mismatch", "Pep Missing", "UniProt Acc"],
        t6_rows,
        col_widths=[26, 16, 16, 16, 22, 20, 74],
    )

    # Save
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(output_path))
    print(f"Report saved: {output_path}")
    print(f"  {len(results)} runs across {len(set(r['model'] for r in results))} models")


def main():
    parser = argparse.ArgumentParser(description="Generate benchmark comparison PDF report")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output PDF path")
    args = parser.parse_args()
    generate_report(args.output)


if __name__ == "__main__":
    main()
