"""Account balances. Bank CSV exports carry no balances, so they come from
<data_dir>/balances.csv, which you maintain by hand:

    Account,As of,Balance,Kind,Min Payment,Due Date,Credit Limit,Interest Rate
    RBC Chequing ...0177,2026-10-15,1109.68,deposit,,,,
    RBC Credit ...3503,2026-09-28,-967.12,card,10.00,2026-10-19,2500.00,20.99

- Balance: at the end of the "As of" day. Card debt is negative.
- Kind: deposit or card (blank = card if the account name contains "Credit").
- Card columns are optional and come from the card statement; Interest Rate is a %.

A bank (deposit) account's latest balance is rolled forward through the transactions
dated after it, so one entry stays current as you download new CSVs. Card balances are
used as entered: Outlook treats them as the statement you have to pay."""
import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

from .models import Balance, Transaction

FILE_NAME = "balances.csv"
COLUMNS = ["Account", "As of", "Balance", "Kind", "Min Payment", "Due Date", "Credit Limit", "Interest Rate"]


def load(path: Path) -> list[Balance]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.DictReader(f), 2):
            row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
            if not row.get("Account"):
                continue
            try:
                out.append(_balance(row, path.name))
            except (ValueError, ArithmeticError, KeyError) as e:   # report and skip the bad line
                print(f"  ! {path.name} line {i}: {e}")
    return out


def _balance(row: dict, source: str) -> Balance:
    dec = lambda k: Decimal(row[k].replace("$", "").replace(",", "")) if row.get(k) else None
    when = lambda k: date.fromisoformat(row[k]) if row.get(k) else None
    kind = row.get("Kind", "").lower() or ("card" if "Credit" in row["Account"] else "deposit")
    if kind not in ("deposit", "card"):
        raise ValueError(f"Kind must be deposit or card, not {kind!r}")
    if dec("Balance") is None or when("As of") is None:
        raise ValueError("As of and Balance are required")
    return Balance(row["Account"], when("As of"), dec("Balance"), source, kind, min_payment=dec("Min Payment"),
                   due_date=when("Due Date"), credit_limit=dec("Credit Limit"), rate=dec("Interest Rate"))


def roll_forward(balances: list[Balance], txns: list[Transaction]) -> list[Balance]:
    """Add, for each deposit account, its latest entered balance plus every later transaction,
    dated at the account's last transaction."""
    latest: dict[str, Balance] = {}
    for b in balances:
        if b.kind == "deposit" and (b.account not in latest or b.date > latest[b.account].date):
            latest[b.account] = b
    out = list(balances)
    for acct, b in latest.items():
        later = [t for t in txns if t.account == acct and t.date > b.date]
        if later:
            out.append(Balance(acct, max(t.date for t in later), b.amount + sum((t.amount for t in later), Decimal(0)),
                               f"{b.source_file} + {len(later)} transactions"))
    return out
