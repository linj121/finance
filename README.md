# Budget sync

Reads bank statements (PDF or CSV) → merges them into one table → assigns a category to
each transaction → writes `transactions.csv` locally and syncs it to the **Transactions**
tab of a Google Sheet. Put your analysis/budget tabs in the same spreadsheet; they
are never overwritten.

## Run

Double-click `run.bat`, or:

```
.venv\Scripts\python -m budget_sync run            # parse, categorize, sync
.venv\Scripts\python -m budget_sync run --no-sync  # local only
.venv\Scripts\python -m budget_sync run --no-ai    # don't call Claude
.venv\Scripts\python -m budget_sync run --reclassify   # after editing rules.yaml
.venv\Scripts\python -m budget_sync run --overwrite    # restore the sheet from transactions.csv, discarding sheet edits
.venv\Scripts\python -m budget_sync inspect <file.pdf> # debug a statement that parses wrong
.venv\Scripts\python -m budget_sync analytics          # rebuild the Analytics + Charts tabs (discards edits on them)
```

Statements are read from `statements_dir` in `config.yaml` (subfolders included).
Re-running is safe: transactions get stable IDs, so nothing is duplicated.

## Categories

Edit `rules.yaml`: the category list, plus keyword rules (first match wins).
Order of precedence:

1. **manual**: you changed the Category cell in Google Sheets (dropdown). Kept forever,
   and remembered for future transactions with the same description.
2. **rule**: a keyword rule in `rules.yaml`
3. **learned**: same description as a transaction you corrected manually
4. **ai**: Claude picks one; the answer is remembered (`category_memory.json`), so each
   merchant is asked once.

Each category also has a parent **group** in `rules.yaml` (`groups:`): Fixed, Essential,
Optional, or Not spending (transfers, card payments, money paid for others: left out of
income and spending). Every category must be in exactly one group.

## Sheet tabs

| Tab | Written by | What it is |
|---|---|---|
| Transactions | every run | all transactions. Edit **Category** (dropdown) or **Note**; both are kept |
| Categories | every run | category list and its group |
| Balances | every run | each statement's closing balance (card debt is negative) |
| Plan | created once by `analytics`, then **never overwritten** | your budgets, savings goal, accounts without statements |
| Analytics, Charts | `analytics` (rebuilt, edits discarded) | live formulas: month vs last month, groups, budget by category, largest expenses, drill-down, 12-month trend, balances and net worth |

In Analytics, pick the month in B4 and the drill-down category next to "Transactions in
category". A month the data doesn't fully cover is labelled partial; an account in Plan
with no balance shows "Missing data" and is left out of net worth. To start Plan over,
delete the tab and run `analytics`.

Every PDF statement is checked: opening balance + parsed transactions must equal the
closing balance, or the file is reported and skipped.

## Cache

Parsed statements are cached in `.cache\`. A statement is re-read only when the file
changes (size or modified time) or the parser code changes, so a normal run takes
about a second instead of a minute.

```
.venv\Scripts\python -m budget_sync run --force     # re-read every statement and re-categorize
                                                    # every record (manual edits are kept)
.venv\Scripts\python -m budget_sync clear-cache     # delete the cache; next run re-reads everything
```
`--force` and `clear-cache` don't re-ask Claude: remembered answers in
`category_memory.json` are reused. Delete that file to have Claude categorize from scratch.

## One-time setup

### Claude (for automatic categorization)
Create a key at https://platform.claude.com, then in PowerShell:
`setx ANTHROPIC_API_KEY "sk-ant-..."` (open a new terminal afterwards).
Model and effort are in `config.yaml`.

### Google Sheets (service account)
1. https://console.cloud.google.com → create a project (e.g. "budget-sync").
2. APIs & Services → Library → enable **Google Sheets API** and **Google Drive API**.
3. IAM & Admin → Service Accounts → Create → no roles needed → open it →
   Keys → Add key → JSON. Save the file as `secrets\service_account.json` here.
4. Open your spreadsheet → Share → add the service account's email
   (`...@...iam.gserviceaccount.com`) as **Editor**.
5. Paste the spreadsheet URL into `google.spreadsheet_url` in `config.yaml`.

## Adding a bank
Add a module in `budget_sync/parsers/` with `detect()` and `parse()` and list it in
`parsers/__init__.py`. `inspect` shows the raw text pdfplumber extracts.
