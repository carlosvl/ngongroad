# ngongroad

Salesforce DX project for Ngong Road.

> **Note:** Project structure inspired by [LLM-Based-SalesforceProject](https://github.com/jvillalpando_sfemu/LLM-Based-SalesforceProject) (used as reference only — this repo is not connected to it).

## Field utilization analysis

Python script that queries your default Salesforce org (via `sf` CLI), measures field population rates, and writes:

- `reports/<ObjectApiName>_field_analysis.md` — full report with Mermaid charts  
- `reports/<ObjectApiName>_field_analysis.pdf` — same content as PDF. **Mermaid charts in the PDF** are rasterized when [Mermaid CLI](https://github.com/mermaid-js/mermaid-cli) is available (`npm i -g @mermaid-js/mermaid-cli`, or `npx` will download it). Without Node/mmdc, diagrams stay as plain text in the PDF; use the `.md` in GitHub/VS Code for interactive charts.

**Setup (PDF is optional):**

```bash
python3 -m pip install -r scripts/python/requirements-analysis.txt
```

(On macOS, `pip` is often missing from your PATH; `python3 -m pip` uses the same Python as `python3`.)

**Run:**

```bash
python3 scripts/python/field_utilization_analysis.py
# or non-interactive (one or many objects, comma-separated):
python3 scripts/python/field_utilization_analysis.py -o Account
python3 scripts/python/field_utilization_analysis.py -o Account,Contact,Record_of_Attendance__c
# Markdown only:
python3 scripts/python/field_utilization_analysis.py -o Account --no-pdf
# PDF without rasterizing Mermaid (faster):
python3 scripts/python/field_utilization_analysis.py -o Account --no-mermaid-pdf
```

Requires the Salesforce CLI authenticated (`sf org login web`, etc.).

### Portfolio summary (all reports)

After you have one or more `reports/*_field_analysis.md` files, roll them into a single executive summary:

```bash
python3 scripts/python/summarize_field_reports.py
```

Writes `reports/field_analysis_portfolio_summary.md` and `reports/field_analysis_portfolio_summary.pdf`. The summary includes a **Contents** section, a **Subreports** table with links to every object’s Markdown and PDF, and the same Mermaid-in-PDF behavior as above (Node + Mermaid CLI for chart images).

Flags: `--no-pdf`, `--no-mermaid-pdf`, `--reports-dir /path/to/folder`.

## Next Steps (Salesforce DX Project)

Now that you’ve created a Salesforce DX project, what’s next? Here are some documentation resources to get you started.

## How Do You Plan to Deploy Your Changes?

Do you want to deploy a set of changes, or create a self-contained application? Choose a [development model](https://developer.salesforce.com/tools/vscode/en/user-guide/development-models).

## Configure Your Salesforce DX Project

The `sfdx-project.json` file contains useful configuration information for your project. See [Salesforce DX Project Configuration](https://developer.salesforce.com/docs/atlas.en-us.sfdx_dev.meta/sfdx_dev/sfdx_dev_ws_config.htm) in the _Salesforce DX Developer Guide_ for details about this file.

## Read All About It

- [Salesforce Extensions Documentation](https://developer.salesforce.com/tools/vscode/)
- [Salesforce CLI Setup Guide](https://developer.salesforce.com/docs/atlas.en-us.sfdx_setup.meta/sfdx_setup/sfdx_setup_intro.htm)
- [Salesforce DX Developer Guide](https://developer.salesforce.com/docs/atlas.en-us.sfdx_dev.meta/sfdx_dev/sfdx_dev_intro.htm)
- [Salesforce CLI Command Reference](https://developer.salesforce.com/docs/atlas.en-us.sfdx_cli_reference.meta/sfdx_cli_reference/cli_reference.htm)
