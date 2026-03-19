#!/usr/bin/env python3
"""Salesforce Field Utilization Analysis

Analyzes field population rates for any Salesforce object and generates
a comprehensive Markdown report with descriptive statistics and Mermaid diagrams,
and optionally a PDF copy of the same content.

Requires: Salesforce CLI (sf) authenticated to the target org.

PDF export (optional): python3 -m pip install -r scripts/python/requirements-analysis.txt
  Mermaid in PDF: rasterized via mmdc / npx @mermaid-js/mermaid-cli when Node is available;
  otherwise diagrams stay as text. Use --no-mermaid-pdf to skip. Open .md for native charts.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import os
import tempfile
from pathlib import Path
import math
import statistics as stats_mod
from collections import Counter, defaultdict
from datetime import datetime

# ── SF CLI helpers ──────────────────────────────────────────────────────────

def run_sf_command(args):
    """Run an sf CLI command and return parsed JSON output."""
    cmd = ["sf"] + args + ["--json"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        try:
            err = json.loads(result.stdout)
            raise RuntimeError(err.get("message", result.stderr))
        except json.JSONDecodeError:
            raise RuntimeError(result.stderr.strip() or f"sf exited with code {result.returncode}")
    return json.loads(result.stdout)


def get_object_describe(object_name):
    print(f"  Fetching metadata for {object_name}...")
    data = run_sf_command(["sobject", "describe", "--sobject", object_name])
    return data.get("result", {})


def get_record_count(object_name):
    print(f"  Counting records...")
    data = run_sf_command(["data", "query", "--query", f"SELECT COUNT() FROM {object_name}"])
    return data.get("result", {}).get("totalSize", 0)


def get_population_counts_batch(object_name, field_names, batch_size=20):
    """Batch COUNT(field) queries — returns {field_name: count}."""
    population = {}
    total_batches = math.ceil(len(field_names) / batch_size)

    for i in range(0, len(field_names), batch_size):
        batch = field_names[i : i + batch_size]
        batch_num = i // batch_size + 1
        print(f"  Population counts — batch {batch_num}/{total_batches} ({len(batch)} fields)")

        select_parts = ", ".join(f"COUNT({f})" for f in batch)
        query = f"SELECT {select_parts} FROM {object_name}"

        try:
            data = run_sf_command(["data", "query", "--query", query])
            record = data.get("result", {}).get("records", [{}])[0]
            for idx, f in enumerate(batch):
                population[f] = record.get(f"expr{idx}", 0)
        except RuntimeError as e:
            print(f"    Batch failed, querying individually: {e}")
            for f in batch:
                population[f] = _individual_count(object_name, f)

    return population


def _individual_count(object_name, field_name):
    """Fallback: count non-null values for one field via WHERE clause."""
    try:
        query = f"SELECT COUNT(Id) cnt FROM {object_name} WHERE {field_name} != null"
        data = run_sf_command(["data", "query", "--query", query])
        return data.get("result", {}).get("records", [{}])[0].get("cnt", 0)
    except RuntimeError:
        return None


# ── Field classification ────────────────────────────────────────────────────

SKIP_TYPES = {"address", "location"}


def classify_fields(fields_meta):
    """Sort fields into query strategy buckets.

    Returns (aggregatable, filterable_non_agg, boolean_fields, skipped).
    - aggregatable:         batch COUNT(field) in SELECT
    - filterable_non_agg:   individual WHERE field != null
    - boolean_fields:       always populated (never null) — no query needed
    - skipped:              compound types or non-filterable long text
    """
    aggregatable, filterable_non_agg, boolean_fields, skipped = [], [], [], []
    for f in fields_meta:
        if f["type"] in SKIP_TYPES:
            skipped.append(f)
        elif f.get("aggregatable"):
            aggregatable.append(f)
        elif f["type"] == "boolean":
            boolean_fields.append(f)
        elif f.get("filterable"):
            filterable_non_agg.append(f)
        else:
            skipped.append(f)
    return aggregatable, filterable_non_agg, boolean_fields, skipped


def is_required(f):
    return (
        not f.get("nillable", True)
        and f.get("createable", False)
        and not f.get("defaultedOnCreate", False)
    )


def utilization_category(rate):
    if rate is None:
        return "Unknown"
    if rate > 95:
        return "Fully Populated"
    if rate > 50:
        return "Well Used"
    if rate > 10:
        return "Under-Utilized"
    if rate > 0:
        return "Rarely Used"
    return "Empty"


CATEGORY_ORDER = [
    "Fully Populated",
    "Well Used",
    "Under-Utilized",
    "Rarely Used",
    "Empty",
]


# ── Statistics ──────────────────────────────────────────────────────────────

def compute_statistics(rates):
    if len(rates) < 2:
        return None

    n = len(rates)
    mean = stats_mod.mean(rates)
    median = stats_mod.median(rates)
    stdev = stats_mod.stdev(rates)
    variance = stats_mod.variance(rates)

    modes = stats_mod.multimode(rates)
    if len(modes) == n:
        mode_str = "No repeating value"
    elif len(modes) <= 3:
        mode_str = ", ".join(f"{m:.1f}%" for m in modes)
    else:
        mode_str = f"{modes[0]:.1f}% (and {len(modes)-1} others)"

    quantiles = stats_mod.quantiles(rates, n=4) if n >= 4 else [median] * 3
    q1, q2, q3 = (quantiles[0], quantiles[1], quantiles[2]) if len(quantiles) == 3 else (median, median, median)

    if n >= 20:
        pctiles = stats_mod.quantiles(rates, n=20)
        p5, p95 = pctiles[0], pctiles[-1]
    else:
        s = sorted(rates)
        p5, p95 = s[0], s[-1]

    if n >= 3 and stdev > 0:
        skewness = (n / ((n - 1) * (n - 2))) * sum(((x - mean) / stdev) ** 3 for x in rates)
    else:
        skewness = 0.0

    if n >= 4 and stdev > 0:
        m4 = sum((x - mean) ** 4 for x in rates) / n
        m2 = sum((x - mean) ** 2 for x in rates) / n
        kurtosis = (m4 / (m2 ** 2)) - 3 if m2 > 0 else 0.0
    else:
        kurtosis = 0.0

    return {
        "n": n,
        "mean": mean,
        "median": median,
        "mode": mode_str,
        "stdev": stdev,
        "variance": variance,
        "min": min(rates),
        "max": max(rates),
        "q1": q1,
        "q3": q3,
        "iqr": q3 - q1,
        "p5": p5,
        "p95": p95,
        "skewness": skewness,
        "kurtosis": kurtosis,
    }


# ── Mermaid generators ─────────────────────────────────────────────────────

def _mermaid_quote(s):
    """Escape double quotes for use inside Mermaid double-quoted strings."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def mermaid_pie(title, data):
    """Mermaid pie syntax: title must be quoted if it contains spaces."""
    t = _mermaid_quote(title)
    lines = ["```mermaid", f'pie title "{t}"']
    for label, value in data.items():
        if value > 0:
            lbl = _mermaid_quote(label)
            lines.append(f'    "{lbl}" : {value}')
    lines.append("```")
    return "\n".join(lines)


def mermaid_bar(title, x_labels, y_label, values, y_max=100):
    x_csv = ", ".join(f'"{l}"' for l in x_labels)
    v_csv = ", ".join(str(round(v, 1)) for v in values)
    return "\n".join([
        "```mermaid",
        "xychart-beta",
        f'    title "{title}"',
        f"    x-axis [{x_csv}]",
        f'    y-axis "{y_label}" 0 --> {y_max}',
        f"    bar [{v_csv}]",
        "```",
    ])


# ── PDF export (optional: markdown + fpdf2) ───────────────────────────────

MERMAID_FENCE_RE = re.compile(r"```mermaid\s*\r?\n(.*?)```", re.DOTALL)


def render_mermaid_to_png(diagram_source: str, out_png: Path) -> bool:
    """Render Mermaid source to PNG using mmdc or npx @mermaid-js/mermaid-cli.

    Returns True if out_png was written. Requires Node/npm for npx path.
    """
    diagram_source = diagram_source.strip()
    if not diagram_source:
        return False

    fd, mmd_str = tempfile.mkstemp(suffix=".mmd", text=True)
    mmd_file = Path(mmd_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(diagram_source)
        out_png = Path(out_png)
        out_png.parent.mkdir(parents=True, exist_ok=True)

        def _run(cmd: list[str]) -> bool:
            try:
                r = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    cwd=str(out_png.parent),
                )
                return r.returncode == 0 and out_png.is_file() and out_png.stat().st_size > 0
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                return False

        if _run(["mmdc", "-i", str(mmd_file), "-o", str(out_png), "-b", "white"]):
            return True
        if _run(
            [
                "npx",
                "-y",
                "@mermaid-js/mermaid-cli",
                "-i",
                str(mmd_file),
                "-o",
                str(out_png),
                "-b",
                "white",
            ]
        ):
            return True
        return False
    finally:
        if mmd_file.is_file():
            mmd_file.unlink()


def _inject_mermaid_images_for_pdf(md: str, tmpdir: Path, enabled: bool) -> tuple[str, int, int]:
    """Replace ```mermaid blocks with markdown images. Returns (md, rendered_count, total_count)."""
    total = len(MERMAID_FENCE_RE.findall(md))
    if not enabled or total == 0:
        return md, 0, total

    state = {"i": 0, "rendered": 0}

    def repl(match: re.Match) -> str:
        code = match.group(1).strip()
        state["i"] += 1
        png = tmpdir / f"mermaid_{state['i']}.png"
        if render_mermaid_to_png(code, png):
            state["rendered"] += 1
            return f"\n\n![Mermaid chart]({png.resolve().as_posix()})\n\n"
        return match.group(0)

    return MERMAID_FENCE_RE.sub(repl, md), state["rendered"], total


def _simplify_markdown_links_for_pdf(md: str) -> str:
    """Turn [label](url) into 'label (url)' so fpdf2 does not choke on <a> inside tables."""
    return re.sub(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", md)


def _normalize_markdown_for_pdf(md: str) -> str:
    """Make Markdown friendlier for fpdf2's HTML renderer (core fonts, no <code> in <td>)."""
    md = (
        md.replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2026", "...")  # ellipsis (core fonts)
    )
    lines = []
    for line in md.splitlines():
        if "|" in line and "`" in line:
            line = re.sub(r"`([^`]+)`", r"\1", line)
        lines.append(line)
    return "\n".join(lines)


def write_pdf_from_markdown(
    markdown_source: str,
    pdf_path: str,
    *,
    render_mermaid: bool = True,
):
    """Write a PDF from the same Markdown as the .md report.

    Returns (success, error_message_or_none, info_note_or_none).

    If ``render_mermaid`` is True, attempts to rasterize `` ```mermaid `` blocks
    to PNG via ``mmdc`` or ``npx @mermaid-js/mermaid-cli`` (Node.js). Diagrams that
    fail to render stay as code in the PDF.
    """
    try:
        import markdown as md_lib
        from fpdf import FPDF
    except ImportError as e:
        return False, (
            f"PDF needs optional packages: python3 -m pip install -r scripts/python/requirements-analysis.txt ({e})"
        ), None

    tmpdir = Path(tempfile.mkdtemp(prefix="sf_mermaid_pdf_"))
    try:
        md_work, m_ok, m_total = _inject_mermaid_images_for_pdf(
            markdown_source, tmpdir, enabled=render_mermaid
        )
        md_prep = _normalize_markdown_for_pdf(md_work)
        md_prep = _simplify_markdown_links_for_pdf(md_prep)
        html = md_lib.markdown(
            md_prep,
            extensions=["tables", "fenced_code"],
        )

        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.set_margins(15, 15, 15)
        pdf.add_page()
        try:
            pdf.write_html(html)
        except Exception as e:
            return False, f"PDF generation failed: {e}", None

        try:
            pdf.output(pdf_path)
        except Exception as e:
            return False, f"Could not write PDF: {e}", None

        note = None
        if m_total > 0:
            if render_mermaid and m_ok == m_total:
                note = f"Mermaid: rendered {m_ok}/{m_total} diagram(s) as images in PDF."
            elif render_mermaid and m_ok > 0:
                note = (
                    f"Mermaid: rendered {m_ok}/{m_total} diagram(s); "
                    f"{m_total - m_ok} left as text (install Node.js and run: "
                    f"npm i -g @mermaid-js/mermaid-cli, or use npx)."
                )
            elif render_mermaid:
                note = (
                    "Mermaid: no diagrams rendered in PDF. Install Node.js, then "
                    "`npm i -g @mermaid-js/mermaid-cli` (or ensure `npx` can run "
                    "`@mermaid-js/mermaid-cli`), or use `--no-mermaid-pdf`."
                )
        return True, None, note
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── Report generation ──────────────────────────────────────────────────────

def _tbl(*cols):
    return "| " + " | ".join(cols) + " |"


def generate_report(object_name, object_label, total_records, fields, st):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    active = [f for f in fields if not f.get("skipped")]
    skipped = [f for f in fields if f.get("skipped")]

    categories = Counter(f["category"] for f in active)
    custom_n = sum(1 for f in active if f["custom"])
    standard_n = len(active) - custom_n
    formula_n = sum(1 for f in active if f["formula"])
    required_n = sum(1 for f in active if f["required"])
    optional_n = len(active) - required_n

    def avg(lst):
        return stats_mod.mean(lst) if lst else 0

    custom_rates = [f["rate"] for f in active if f["custom"] and f["rate"] is not None]
    standard_rates = [f["rate"] for f in active if not f["custom"] and f["rate"] is not None]
    required_rates = [f["rate"] for f in active if f["required"] and f["rate"] is not None]
    optional_rates = [f["rate"] for f in active if not f["required"] and f["rate"] is not None]

    type_buckets = defaultdict(list)
    for f in active:
        if f["rate"] is not None:
            type_buckets[f["type"]].append(f["rate"])
    type_avg = sorted(
        ((t, avg(r), len(r)) for t, r in type_buckets.items()),
        key=lambda x: -x[1],
    )

    L = []  # report lines
    w = L.append

    # ── Title & timestamp ───────────────────────────────────────────────
    w(f"# Field Utilization Analysis: {object_label} (`{object_name}`)\n")
    w(f"> Generated on {now}\n")

    # ── Executive Summary ───────────────────────────────────────────────
    w("## Executive Summary\n")
    w(_tbl("Metric", "Value"))
    w(_tbl("---", "---"))
    w(_tbl("**Object**", f"{object_label} (`{object_name}`)"))
    w(_tbl("**Total Records**", f"{total_records:,}"))
    w(_tbl("**Total Fields Analyzed**", str(len(active))))
    w(_tbl("**Standard / Custom**", f"{standard_n} / {custom_n}"))
    w(_tbl("**Formula / Calculated**", str(formula_n)))
    w(_tbl("**Required / Optional**", f"{required_n} / {optional_n}"))
    if st:
        w(_tbl("**Mean Population Rate**", f"{st['mean']:.1f}%"))
        w(_tbl("**Median Population Rate**", f"{st['median']:.1f}%"))
    w("")

    # ── Utilization Category Distribution ───────────────────────────────
    w("## Utilization Category Distribution\n")
    w(_tbl("Category", "Threshold", "Fields", "% of Total"))
    w(_tbl("---", "---", "---", "---"))
    thresholds = {
        "Fully Populated": "> 95 %",
        "Well Used": "50 – 95 %",
        "Under-Utilized": "10 – 50 %",
        "Rarely Used": "1 – 10 %",
        "Empty": "0 %",
    }
    for cat in CATEGORY_ORDER:
        cnt = categories.get(cat, 0)
        pct = cnt / len(active) * 100 if active else 0
        w(_tbl(cat, thresholds[cat], str(cnt), f"{pct:.1f}%"))
    w("")
    w(mermaid_pie("Field Utilization Categories", {c: categories.get(c, 0) for c in CATEGORY_ORDER}))
    w("")

    # ── Descriptive Statistics ──────────────────────────────────────────
    if st:
        w("## Descriptive Statistics\n")
        w("Population-rate statistics across all analyzed fields:\n")
        w(_tbl("Statistic", "Value"))
        w(_tbl("---", "---"))
        for label, key, fmt in [
            ("N (fields)", "n", "d"),
            ("Mean", "mean", ".2f"),
            ("Median", "median", ".2f"),
            ("Std Dev", "stdev", ".2f"),
            ("Variance", "variance", ".2f"),
            ("Min", "min", ".2f"),
            ("Max", "max", ".2f"),
            ("Q1 (25th pctl)", "q1", ".2f"),
            ("Q3 (75th pctl)", "q3", ".2f"),
            ("IQR", "iqr", ".2f"),
            ("5th Percentile", "p5", ".2f"),
            ("95th Percentile", "p95", ".2f"),
            ("Skewness", "skewness", ".3f"),
            ("Excess Kurtosis", "kurtosis", ".3f"),
        ]:
            v = st[key]
            suffix = "%" if key not in ("n", "skewness", "kurtosis", "variance") else ""
            w(_tbl(label, f"{v:{fmt}}{suffix}" if isinstance(v, (int, float)) else str(v)))
        w(_tbl("Mode", st["mode"]))
        w("")

        w("**Interpretation:**\n")
        sk = st["skewness"]
        if sk > 0.5:
            w(f"- **Skewness ({sk:.3f})** — Right-skewed: most fields cluster at lower population rates with a few highly populated outliers.")
        elif sk < -0.5:
            w(f"- **Skewness ({sk:.3f})** — Left-skewed: most fields are well-populated; a small tail of under-populated fields exists.")
        else:
            w(f"- **Skewness ({sk:.3f})** — Approximately symmetric distribution of population rates.")

        ku = st["kurtosis"]
        if ku > 1:
            w(f"- **Kurtosis ({ku:.3f})** — Leptokurtic: heavy tails and a sharp peak — population rates concentrate tightly with notable outliers.")
        elif ku < -1:
            w(f"- **Kurtosis ({ku:.3f})** — Platykurtic: light tails and a flat peak — population rates are broadly spread.")
        else:
            w(f"- **Kurtosis ({ku:.3f})** — Mesokurtic: distribution shape is close to normal.")
        w("")

    # ── Utilization by Field Type ───────────────────────────────────────
    w("## Utilization by Field Type\n")
    w(_tbl("Field Type", "Count", "Avg Population Rate"))
    w(_tbl("---", "---", "---"))
    for t, a, c in type_avg:
        w(_tbl(t, str(c), f"{a:.1f}%"))
    w("")

    if type_avg:
        top = type_avg[:12]
        w(mermaid_bar(
            "Average Population Rate by Field Type",
            [t for t, _, _ in top],
            "Population %",
            [a for _, a, _ in top],
        ))
        w("")

    # ── Standard vs Custom ──────────────────────────────────────────────
    w("## Standard vs Custom Field Comparison\n")
    w(_tbl("Segment", "Fields", "Avg Population Rate"))
    w(_tbl("---", "---", "---"))
    w(_tbl("Standard", str(standard_n), f"{avg(standard_rates):.1f}%"))
    w(_tbl("Custom", str(custom_n), f"{avg(custom_rates):.1f}%"))
    w("")
    w(mermaid_pie("Standard vs Custom Fields", {"Standard": standard_n, "Custom": custom_n}))
    w("")

    # ── Required vs Optional ────────────────────────────────────────────
    w("## Required vs Optional Fields\n")
    w(_tbl("Segment", "Fields", "Avg Population Rate"))
    w(_tbl("---", "---", "---"))
    w(_tbl("Required", str(required_n), f"{avg(required_rates):.1f}%"))
    w(_tbl("Optional", str(optional_n), f"{avg(optional_rates):.1f}%"))
    w("")
    w(mermaid_pie("Required vs Optional Fields", {"Required": required_n, "Optional": optional_n}))
    w("")

    # ── Detailed Field Tables ───────────────────────────────────────────
    w("## Detailed Field Analysis\n")

    header = _tbl("Field API Name", "Label", "Type", "Population", "Rate", "Custom", "Required", "Formula")
    sep = _tbl("---", "---", "---", "---", "---", "---", "---", "---")

    for cat in CATEGORY_ORDER:
        cat_fields = sorted(
            [f for f in active if f["category"] == cat],
            key=lambda f: (f["rate"] is None, -(f["rate"] or 0)),
        )
        if not cat_fields:
            continue
        w(f"### {cat} ({len(cat_fields)} fields)\n")
        w(header)
        w(sep)
        for f in cat_fields:
            pop = f"{f['pop']:,}" if f["pop"] is not None else "N/A"
            rate = f"{f['rate']:.1f}%" if f["rate"] is not None else "N/A"
            w(_tbl(
                f"`{f['name']}`", f["label"], f["type"],
                pop, rate,
                "Yes" if f["custom"] else "", "Yes" if f["required"] else "", "Yes" if f["formula"] else "",
            ))
        w("")

    if skipped:
        w("### Skipped Fields (compound / non-queryable)\n")
        w(_tbl("Field API Name", "Label", "Type"))
        w(_tbl("---", "---", "---"))
        for f in skipped:
            w(_tbl(f"`{f['name']}`", f["label"], f["type"]))
        w("")

    # ── Recommendations ─────────────────────────────────────────────────
    w("## Recommendations\n")

    deletion = [f for f in active if f["custom"] and f["category"] == "Empty" and not f["required"] and not f["formula"]]
    w("### Fields Recommended for Deletion Review\n")
    if deletion:
        w("These **custom** fields have **0 % population**, are not required, and are not formula fields.")
        w("They are strong candidates for removal after confirming they are not referenced in automation, reports, or integrations.\n")
        for f in deletion:
            w(f"- `{f['name']}` ({f['label']}) — {f['type']}")
    else:
        w("No custom fields with 0 % population found — all custom fields contain at least some data.")
    w("")

    strategy = sorted(
        [f for f in active if f["rate"] is not None and 0 < f["rate"] <= 25 and not f["formula"] and f.get("updateable", True)],
        key=lambda f: f["rate"],
    )
    w("### Fields Needing a Data Collection Strategy\n")
    if strategy:
        w("These fields are **< 25 % populated** and user-editable. Evaluate whether the data is valuable;")
        w("if so, consider validation rules, required-field configuration, screen flows, or training to improve collection.\n")
        w(_tbl("Field", "Label", "Type", "Rate", "Custom"))
        w(_tbl("---", "---", "---", "---", "---"))
        for f in strategy:
            w(_tbl(f"`{f['name']}`", f["label"], f["type"], f"{f['rate']:.1f}%", "Yes" if f["custom"] else ""))
    else:
        w("All user-editable fields are above 25 % population — no immediate data-collection gaps identified.")
    w("")

    gaps = [f for f in active if f["required"] and f["rate"] is not None and f["rate"] < 100]
    if gaps:
        w("### Required Fields with Unexpected Gaps\n")
        w("These fields are marked as required but have < 100 % population, possibly due to")
        w("historical records created before the requirement was enforced.\n")
        for f in gaps:
            w(f"- `{f['name']}` ({f['label']}) — {f['rate']:.1f}% populated")
        w("")

    w("---\n")
    w(f"*Analysis performed on {now} against `{object_name}` with {total_records:,} records.*\n")

    return "\n".join(L)


# ── Main ────────────────────────────────────────────────────────────────────

def parse_object_names(raw: str) -> list[str]:
    """Split comma-separated API names; strip whitespace; drop empties."""
    return [p.strip() for p in raw.split(",") if p.strip()]


def analyze_single_object(object_name: str, args) -> bool:
    """Run full analysis for one object. Returns True on success, False on failure."""
    print(f"Analyzing `{object_name}`...")
    print("-" * 40)

    try:
        describe = get_object_describe(object_name)
    except RuntimeError as e:
        print(f"  Error describing object: {e}")
        return False

    object_label = describe.get("label", object_name)
    all_fields = describe.get("fields", [])
    print(f"  Found {len(all_fields)} fields")

    try:
        total_records = get_record_count(object_name)
    except RuntimeError as e:
        print(f"  Error counting records: {e}")
        return False
    print(f"  Found {total_records:,} records")

    aggregatable, filterable_non_agg, boolean_fields, skipped = classify_fields(all_fields)
    print(f"  Aggregatable: {len(aggregatable)} | Filterable (non-agg): {len(filterable_non_agg)}"
          f" | Boolean: {len(boolean_fields)} | Skipped: {len(skipped)}")
    print()

    population = {}
    if total_records > 0:
        agg_names = [f["name"] for f in aggregatable]
        population = get_population_counts_batch(object_name, agg_names)

        if filterable_non_agg:
            print(f"  Querying {len(filterable_non_agg)} filterable non-aggregatable fields individually...")
        for f in filterable_non_agg:
            population[f["name"]] = _individual_count(object_name, f["name"])

        for f in boolean_fields:
            population[f["name"]] = total_records
    else:
        for f in aggregatable + filterable_non_agg + boolean_fields:
            population[f["name"]] = 0

    def _build_field_record(f):
        name = f["name"]
        cnt = population.get(name)
        rate = (cnt / total_records * 100) if cnt is not None and total_records > 0 else (0.0 if cnt == 0 else None)
        return {
            "name": name,
            "label": f.get("label", name),
            "type": f.get("type", "unknown"),
            "custom": f.get("custom", False),
            "formula": bool(f.get("calculated", False)),
            "required": is_required(f),
            "updateable": f.get("updateable", False),
            "pop": cnt,
            "rate": rate,
            "category": utilization_category(rate),
        }

    fields = [_build_field_record(f) for f in aggregatable + filterable_non_agg + boolean_fields]
    for f in skipped:
        fields.append({
            "name": f["name"],
            "label": f.get("label", f["name"]),
            "type": f.get("type", "unknown"),
            "custom": f.get("custom", False),
            "formula": bool(f.get("calculated", False)),
            "required": False,
            "updateable": False,
            "pop": None,
            "rate": None,
            "category": "Skipped",
            "skipped": True,
        })

    rates = [f["rate"] for f in fields if f["rate"] is not None]
    st = compute_statistics(rates)

    print("\n  Generating report...")
    report = generate_report(object_name, object_label, total_records, fields, st)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(script_dir))
    reports_dir = os.path.join(project_root, "reports")
    os.makedirs(reports_dir, exist_ok=True)

    filepath = os.path.join(reports_dir, f"{object_name}_field_analysis.md")
    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write(report)

    print(f"\n  Report saved to: {filepath}")

    if not args.no_pdf:
        pdf_path = os.path.splitext(filepath)[0] + ".pdf"
        ok, err, note = write_pdf_from_markdown(
            report,
            pdf_path,
            render_mermaid=not args.no_mermaid_pdf,
        )
        if ok:
            print(f"  PDF saved to: {pdf_path}")
            if note:
                print(f"  {note}")
        else:
            print(f"  PDF not created: {err}")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Analyze Salesforce field utilization; write Markdown (+ optional PDF).",
    )
    parser.add_argument(
        "-o",
        "--object",
        dest="object_name",
        help="Salesforce object API name(s), comma-separated (e.g. Account,Contact,My__c). "
        "If omitted, you will be prompted (comma-separated input allowed).",
    )
    parser.add_argument(
        "--no-pdf",
        action="store_true",
        help="Skip PDF export (only write .md)",
    )
    parser.add_argument(
        "--no-mermaid-pdf",
        action="store_true",
        help="Do not rasterize Mermaid diagrams in PDF (faster; charts as text)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  Salesforce Field Utilization Analysis")
    print("=" * 60)
    print()

    try:
        subprocess.run(["sf", "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: Salesforce CLI (sf) is not installed or not in PATH.")
        sys.exit(1)

    raw = (args.object_name or "").strip()
    if not raw:
        raw = input(
            "Enter object API name(s), comma-separated (e.g. Account, Contact, Custom__c): "
        ).strip()
    objects = parse_object_names(raw)
    if not objects:
        print("Error: no object name(s) provided.")
        sys.exit(1)

    if len(objects) > 1:
        print(f"Batch mode: {len(objects)} object(s) — {', '.join(objects)}\n")

    failures: list[str] = []
    for idx, object_name in enumerate(objects, start=1):
        if len(objects) > 1:
            print(f"\n{'=' * 60}\n  [{idx}/{len(objects)}] `{object_name}`\n{'=' * 60}")
        if not analyze_single_object(object_name, args):
            failures.append(object_name)

    print()
    if failures:
        print(f"Finished with errors — failed ({len(failures)}): {', '.join(failures)}")
        sys.exit(1)
    if len(objects) > 1:
        print(f"Done — successfully processed {len(objects)} object(s).")
    else:
        print("Done!")


if __name__ == "__main__":
    main()
