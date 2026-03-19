#!/usr/bin/env python3
"""Aggregate all `*_field_analysis.md` reports into one portfolio summary (.md + .pdf).

Scans the reports directory (default: repo `reports/`), parses executive summaries
and utilization categories from each file, and writes:
  - field_analysis_portfolio_summary.md  (Contents, Subreports link hub, rollups, Mermaid)
  - field_analysis_portfolio_summary.pdf (same; Mermaid as images if Node + mermaid-cli available)

Python PDF deps: python3 -m pip install -r scripts/python/requirements-analysis.txt
Mermaid-in-PDF: npm i -g @mermaid-js/mermaid-cli (or npx @mermaid-js/mermaid-cli)
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

# Reuse PDF writer from sibling module
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from field_utilization_analysis import mermaid_bar, mermaid_pie, write_pdf_from_markdown

SUMMARY_MD_BASENAME = "field_analysis_portfolio_summary"
INPUT_GLOB = "*_field_analysis.md"


def _heading_anchor(title: str) -> str:
    """Approximate GitHub-style fragment IDs for same-repo linking."""
    t = title.lower().replace("—", "-").replace("–", "-")
    t = re.sub(r"[^a-z0-9\s-]+", "", t)
    t = re.sub(r"[\s_]+", "-", t)
    return re.sub(r"-+", "-", t).strip("-")


def _report_links(r: dict) -> str:
    """Markdown links to subreport .md and .pdf (if present)."""
    md_part = f"[Markdown](./{r['filename']})"
    if r.get("has_pdf"):
        return f"{md_part} · [PDF](./{r['pdf_filename']})"
    return f"{md_part} · *PDF not generated*"


def _strip_md_bold(s: str) -> str:
    return re.sub(r"\*\*([^*]+)\*\*", r"\1", s).strip()


def _parse_md_table(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            continue
        if all(re.fullmatch(r"-+", c.strip()) for c in cells if c.strip()):
            continue
        rows.append(cells)
    return rows


def _slice_section(md: str, heading: str) -> str:
    """Return body after `heading` (including ##) until the next ## heading."""
    idx = md.find(heading)
    if idx < 0:
        return ""
    start = idx + len(heading)
    rest = md[start:]
    m = re.search(r"^## ", rest, re.MULTILINE)
    if m:
        return rest[: m.start()].strip()
    return rest.strip()


def _parse_num(s: str):
    s = str(s).replace(",", "").replace("%", "").strip()
    if not s:
        return None
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return None


def parse_field_report(md: str, filename: str) -> dict | None:
    """Extract structured data from one field analysis markdown body."""
    m = re.search(
        r"^# Field Utilization Analysis: (.+?) \(`([^`]+)`\)\s*$",
        md,
        re.MULTILINE,
    )
    if not m:
        return None
    label, api = m.group(1).strip(), m.group(2).strip()

    gen_m = re.search(r"^> Generated on (.+)$", md, re.MULTILINE)
    generated = gen_m.group(1).strip() if gen_m else ""

    exec_body = _slice_section(md, "## Executive Summary")
    exec_rows = _parse_md_table(exec_body)
    executive: dict[str, str] = {}
    for row in exec_rows:
        if len(row) >= 2:
            executive[_strip_md_bold(row[0])] = row[1].strip()

    cat_body = _slice_section(md, "## Utilization Category Distribution")
    # Drop mermaid / code fences from category section
    cat_body = re.split(r"```", cat_body)[0]
    cat_rows = _parse_md_table(cat_body)
    categories: dict[str, dict[str, float | int]] = {}
    for row in cat_rows:
        if len(row) >= 4:
            name = row[0].strip()
            try:
                count = int(row[2].strip().replace(",", ""))
            except ValueError:
                continue
            pct = _parse_num(row[3])
            categories[name] = {"count": count, "pct": pct}

    # Deletion candidates: lines like "- `ApiName__c` (Label) — type"
    rec_body = _slice_section(md, "## Recommendations")
    del_m = re.search(
        r"### Fields Recommended for Deletion Review\s*\n+(.*?)(?=### |\Z)",
        rec_body,
        re.DOTALL,
    )
    deletion_count = 0
    if del_m:
        block = del_m.group(1)
        if re.search(r"No custom fields", block, re.I):
            deletion_count = 0
        else:
            deletion_count = len(re.findall(r"^- `", block, re.MULTILINE))

    return {
        "filename": filename,
        "api": api,
        "label": label,
        "generated": generated,
        "executive": executive,
        "categories": categories,
        "deletion_candidates": deletion_count,
    }


def _exec_get(exec_map: dict, *keys: str) -> str:
    for k in keys:
        if k in exec_map:
            return exec_map[k]
    return ""


def build_summary_markdown(
    reports: list[dict],
    generated_at: str,
    reports_dir: Path,
) -> str:
    lines: list[str] = []
    w = lines.append

    w("# Field utilization — portfolio summary\n")
    w(f"> Generated on {generated_at}\n")
    w(
        f"This document rolls up **{len(reports)}** object report(s) from `{INPUT_GLOB}` "
        f"in [`{reports_dir.name}/`](./). Use **Subreports** below for direct links to each analysis "
        "(Markdown + PDF). "
        "For PDFs with **rendered Mermaid charts**, install [Mermaid CLI](https://github.com/mermaid-js/mermaid-cli) "
        "(`npm i -g @mermaid-js/mermaid-cli`) or use `npx`; then re-run the analysis or summary script.\n"
    )

    # --- Contents ---
    w("## Contents\n")
    toc_titles = [
        "Subreports",
        "Objects at a glance",
        "Portfolio rollups",
        "Mean population rate by object",
        "Field counts by utilization category (summed across objects)",
        "Per-object snapshot",
    ]
    for t in toc_titles:
        w(f"- [{t}](#{_heading_anchor(t)})")
    w("")

    # --- Subreports (link hub) ---
    w("## Subreports\n")
    w("| Object (API) | Markdown | PDF |")
    w("| --- | --- | --- |")
    for r in sorted(reports, key=lambda x: x["api"].lower()):
        md_l = f"[{r['filename']}](./{r['filename']})"
        if r.get("has_pdf"):
            pdf_l = f"[{r['pdf_filename']}](./{r['pdf_filename']})"
        else:
            pdf_l = "—"
        w(f"| `{r['api']}` | {md_l} | {pdf_l} |")
    w("")

    # --- At a glance ---
    w("## Objects at a glance\n")
    w(
        "| Object (API) | Label | Records | Fields | Std / Cust | Mean pop % | Median pop % | "
        "Empty | Deletion candidates* | Reports |"
    )
    w("| --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- |")
    total_records = 0
    total_fields = 0
    weighted_mean_num = 0.0
    empty_sum = 0
    means_for_median: list[float] = []

    for r in sorted(reports, key=lambda x: x["api"].lower()):
        ex = r["executive"]
        rec_s = _exec_get(ex, "Total Records")
        fld_s = _exec_get(ex, "Total Fields Analyzed")
        sc_s = _exec_get(ex, "Standard / Custom")
        mean_s = _exec_get(ex, "Mean Population Rate")
        med_s = _exec_get(ex, "Median Population Rate")
        n_rec = int(rec_s.replace(",", "")) if rec_s.replace(",", "").isdigit() else 0
        n_fld = int(fld_s) if str(fld_s).isdigit() else 0
        mean_v = _parse_num(mean_s)
        med_v = _parse_num(med_s)
        empty_n = int(r["categories"].get("Empty", {}).get("count", 0))
        link = _report_links(r)
        w(
            "| {api} | {label} | {rec} | {fld} | {sc} | {mean} | {med} | {empty} | {delcnt} | {link} |".format(
                api=r["api"],
                label=r["label"][:40] + ("..." if len(r["label"]) > 40 else ""),
                rec=rec_s or "-",
                fld=fld_s or "-",
                sc=sc_s or "-",
                mean=mean_s or "-",
                med=med_s or "-",
                empty=empty_n,
                delcnt=r["deletion_candidates"],
                link=link,
            )
        )
        try:
            tr = int(str(rec_s).replace(",", ""))
            total_records += tr
        except ValueError:
            pass
        try:
            tf = int(str(fld_s))
            total_fields += tf
            if mean_v is not None and tf:
                weighted_mean_num += float(mean_v) * tf
                means_for_median.append(float(mean_v))
        except ValueError:
            pass
        empty_sum += empty_n

    w("")
    w("*Deletion candidates = custom fields at 0% population flagged in each report (review before removing).*\n")

    # --- Portfolio rollups ---
    w("## Portfolio rollups\n")
    w("| Metric | Value |")
    w("| --- | --- |")
    w(f"| Objects analyzed | {len(reports)} |")
    w(f"| Sum of records (all objects) | {total_records:,} |")
    w(f"| Sum of fields analyzed | {total_fields:,} |")
    if total_fields > 0 and weighted_mean_num > 0:
        wma = weighted_mean_num / total_fields
        w(f"| Field-weighted mean population rate | {wma:.1f}% |")
    if means_for_median:
        w(f"| Median of per-object mean population rates | {statistics.median(means_for_median):.1f}% |")
    w(f"| Total fields in Empty category (summed across objects) | {empty_sum:,} |")
    w("")

    # --- Mermaid: mean population by object ---
    if len(reports) >= 1:
        short = lambda s: (s[:14] + "..." if len(s) > 14 else s).replace('"', "'")
        apis = [short(r["api"]) for r in sorted(reports, key=lambda x: x["api"].lower())]
        means = []
        for r in sorted(reports, key=lambda x: x["api"].lower()):
            mv = _parse_num(_exec_get(r["executive"], "Mean Population Rate"))
            means.append(float(mv) if mv is not None else 0.0)
        w("## Mean population rate by object\n")
        if len(apis) <= 12:
            w(mermaid_bar("Mean population rate by object", apis, "Mean %", means))
        else:
            w("*Too many objects for a single chart; showing first 12 alphabetically.*\n")
            w(mermaid_bar("Mean population rate (first 12 objects)", apis[:12], "Mean %", means[:12]))
        w("")

    # --- Stacked category view (totals) ---
    cat_names = [
        "Fully Populated",
        "Well Used",
        "Under-Utilized",
        "Rarely Used",
        "Empty",
    ]
    totals = {c: sum(r["categories"].get(c, {}).get("count", 0) for r in reports) for c in cat_names}
    if any(totals.values()):
        w("## Field counts by utilization category (summed across objects)\n")
        w("| Category | Total fields |")
        w("| --- | ---: |")
        for c in cat_names:
            if totals[c]:
                w(f"| {c} | {totals[c]:,} |")
        w("")
        w(mermaid_pie("Portfolio field counts by category", totals))
        w("")

    # --- Per-object snapshot ---
    w("## Per-object snapshot\n")
    for r in sorted(reports, key=lambda x: x["api"].lower()):
        ex = r["executive"]
        w(f"### {r['label']} (`{r['api']}`)\n")
        w(
            f"- **Markdown:** [{r['filename']}](./{r['filename']})"
            + (
                f" · **PDF:** [{r['pdf_filename']}](./{r['pdf_filename']})"
                if r.get("has_pdf")
                else " · **PDF:** *not generated — run field analysis with PDF export enabled*"
            )
            + f" (source generated {r['generated'] or 'unknown'})"
        )
        w(
            f"- Records: {_exec_get(ex, 'Total Records')}; fields analyzed: {_exec_get(ex, 'Total Fields Analyzed')}; "
            f"mean / median population: {_exec_get(ex, 'Mean Population Rate')} / {_exec_get(ex, 'Median Population Rate')}"
        )
        cats = r["categories"]
        parts = [f"{k}: {cats[k]['count']}" for k in cat_names if cats.get(k, {}).get("count")]
        if parts:
            w("- Categories: " + "; ".join(parts))
        w("")

    w("---\n")
    w("*End of portfolio summary.*\n")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize all *_field_analysis.md reports into one .md + .pdf.")
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=None,
        help="Folder containing *_field_analysis.md (default: <repo>/reports)",
    )
    parser.add_argument("--no-pdf", action="store_true", help="Skip PDF output")
    parser.add_argument(
        "--no-mermaid-pdf",
        action="store_true",
        help="Do not rasterize Mermaid diagrams in the portfolio PDF",
    )
    args = parser.parse_args()

    project_root = _SCRIPT_DIR.parent.parent
    reports_dir = args.reports_dir or (project_root / "reports")
    reports_dir = reports_dir.resolve()

    if not reports_dir.is_dir():
        print(f"Error: reports directory not found: {reports_dir}", file=sys.stderr)
        return 1

    paths = sorted(
        p
        for p in reports_dir.glob(INPUT_GLOB)
        if p.is_file() and not p.name.startswith(SUMMARY_MD_BASENAME)
    )
    if not paths:
        print(f"No reports matching {INPUT_GLOB} in {reports_dir}", file=sys.stderr)
        return 1

    parsed: list[dict] = []
    for p in paths:
        text = p.read_text(encoding="utf-8")
        row = parse_field_report(text, p.name)
        if row:
            row["pdf_filename"] = p.with_suffix(".pdf").name
            row["has_pdf"] = (reports_dir / row["pdf_filename"]).is_file()
            parsed.append(row)
        else:
            print(f"Warning: could not parse {p.name}, skipping", file=sys.stderr)

    if not parsed:
        print("No valid field analysis reports could be parsed.", file=sys.stderr)
        return 1

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md_out = build_summary_markdown(parsed, generated_at, reports_dir)

    md_path = reports_dir / f"{SUMMARY_MD_BASENAME}.md"
    md_path.write_text(md_out, encoding="utf-8")
    print(f"Wrote {md_path}")

    if not args.no_pdf:
        pdf_path = reports_dir / f"{SUMMARY_MD_BASENAME}.pdf"
        ok, err, note = write_pdf_from_markdown(
            md_out,
            str(pdf_path),
            render_mermaid=not args.no_mermaid_pdf,
        )
        if ok:
            print(f"Wrote {pdf_path}")
            if note:
                print(note)
        else:
            print(f"PDF not created: {err}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
