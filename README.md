# Budget sync

Reads bank CSV exports → merges them into one table → assigns a category to
each transaction → writes `transactions.csv` locally and syncs it to the **Transactions**
tab of a Google Sheet. Put your analysis/budget tabs in the same spreadsheet; they
are never overwritten.

## Run

Double-click `run.bat`, or:

```
.venv\Scripts\python -m budget_sync run            # read CSVs, categorize, sync
.venv\Scripts\python -m budget_sync run --no-sync  # local only
.venv\Scripts\python -m budget_sync run --no-ai    # don't call Claude
.venv\Scripts\python -m budget_sync run --reclassify   # after editing rules.yaml
.venv\Scripts\python -m budget_sync run --overwrite    # restore the sheet from transactions.csv, discarding sheet edits
.venv\Scripts\python -m budget_sync analytics          # rebuild the Analytics + Charts tabs (discards edits on them)
```

## Data

All private data lives in `data\` (git-ignored, never pushed). Every `*.csv` under it,
subfolders included, is read; a file is recognised by its columns, so names and folders
are up to you (e.g. `data\rbc\debit\download-transactions.csv`). Download from RBC Online
Banking → Account Services → Download (CSV). Downloads may overlap: a transaction found in
several files is counted once. Re-running is safe: transactions get stable IDs.

Bank CSVs have no balances, so put them in `data\balances.csv`:

```
Account,As of,Balance,Kind,Min Payment,Due Date,Credit Limit,Interest Rate
RBC Chequing ...0177,2026-10-15,1109.68,deposit,,,,
RBC Credit ...3503,2026-09-28,-967.12,card,10.00,2026-10-19,2500.00,20.99
```

Balance is at the end of the As of day; **card debt is negative** (a positive card balance
is reported as a warning). Add a card row from each new card statement: Outlook pays that
statement's balance. One row per bank account is enough. From these rows and the
transactions, every run also works out each account's current balance and its balance on
every month end (for the cash and net-worth trends). Account names are
`RBC <Chequing|Saving|Credit> ...<last 4 digits>`.

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
| Balances | every run | rows of `data\balances.csv`, plus derived rows: each account's current balance (`deposit` / `card current`) and month-end balances (`… month-end`). Card debt is negative |
| Plan | created once by `analytics`, then **never overwritten** | your budgets, savings goal, accounts without statements |
| Analytics, Charts | `analytics` (rebuilt, edits discarded) | live formulas. A scorecard of four habits over the last 12 complete months (savings rate ≥ 20%, emergency fund ≥ 3 months of essential spending, no card interest, growing net worth); the picked month vs the one before; groups vs budget; balances; 12-month trend; spending by category; drill-down. Charts: one per habit |
| Bills & Goals | created once by `analytics`, then **never overwritten** | recurring income and bills (pre-filled with suggestions from your history, unconfirmed), one-off costs to save for, savings goals, emergency-fund settings |
| Outlook | `analytics` (rebuilt) | next 30 days of cash, recurring commitments, emergency fund, where your cash stands, credit card, one-off costs, goals, "can I afford my plans?" |

Only items ticked **Confirmed** on Bills & Goals count in the forecast. The forecast starts at
the date of your latest bank transaction; to start from today, type today's date and cash there.

In Analytics, pick the month in B4 and the drill-down category next to "Transactions in
category". A month the data doesn't fully cover is labelled partial; an account in Plan
with no balance shows "Missing data" and is left out of net worth. To start Plan over,
delete the tab and run `analytics`.

`--reclassify` doesn't re-ask Claude: remembered answers in `category_memory.json` are
reused. Delete that file to have Claude categorize from scratch.

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
Add a module in `budget_sync/parsers/` with `detect(header)` (does this CSV's column list
belong to that bank?) and `parse(path, source)`, and list it in `parsers/__init__.py`.
