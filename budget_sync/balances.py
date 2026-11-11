"""Account balances. Bank CSV exports carry no balances, so they come from
<data_dir>/balances.csv, which you maintain by hand:

    Account,As of,Balance,Kind,Min Payment,Due Date,Credit Limit,Interest Rate
    RBC Chequing ...0177,2026-10-15,1109.68,deposit,,,,
    RBC Credit ...3503,2026-09-28,-967.12,card,10.00,2026-10-19,2500.00,20.99

- Balance: at the end of the "As of" day. Card debt is negative.
- Kind: deposit or card (blank = card if the account name contains "Credit").
- Card columns are optional and come from the card statement; Interest Rate is a %.

From the latest entered balance of each account and its transactions, derive() adds:
- deposit:        a bank account's balance at its last transaction, so one entry stays
                  current as you download new CSVs (Outlook's starting cash)
- card current:   the same for a card. Kept apart from 'card' rows, which Outlook treats
                  as the statement you have to pay
- deposit month-end / card month-end: every account's balance on each month's last day,
                  for the cash and net worth trends in Analytics
Formulas that want an account's latest balance skip the month-end rows."""
import csv
from datetime import date, timedelta
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
                continue
            if out[-1].kind == "card" and out[-1].amount > 0:
                print(f"  ! {path.name} line {i}: card balance {out[-1].amount} is positive, i.e. the bank "
                      f"owes you. If it's what you owe, enter it as negative")
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


MONTH_END = " month-end"
CURRENT = {"deposit": "deposit", "card": "card current"}


def derive(balances: list[Balance], txns: list[Transaction]) -> list[Balance]:
    """The entered balances plus each account's current and month-end balances. A date's
    balance starts from the nearest entered balance on or before it (+ the transactions
    since), or before the first one, from that one (- the transactions in between). Card
    statements can leave out a purchase dated on the statement day, so starting near the
    date keeps such a gap from spreading over the whole history."""
    by_acct: dict[str, list[Balance]] = {}
    for b in sorted(balances, key=lambda b: b.date):
        by_acct.setdefault(b.account, []).append(b)
    out = list(balances)
    for acct, snaps in by_acct.items():
        mine = sorted((t for t in txns if t.account == acct), key=lambda t: t.date)
        if not mine:
            continue

        def at(d: date) -> tuple[Decimal, Balance]:
            b = next((s for s in reversed(snaps) if s.date <= d), snaps[0])
            if d >= b.date:
                return b.amount + sum((t.amount for t in mine if b.date < t.date <= d), Decimal(0)), b
            return b.amount - sum((t.amount for t in mine if d < t.date <= b.date), Decimal(0)), b

        def row(d: date, kind: str) -> Balance:
            amount, b = at(d)
            return Balance(acct, d, amount, f"{b.source_file} ({b.date}) ± transactions", kind)

        last, kind = mine[-1].date, snaps[-1].kind
        if last > snaps[-1].date:
            out.append(row(last, CURRENT[kind]))
        # month ends from the first transaction's month up to the last one fully covered
        d = _month_end(mine[0].date)
        while d <= last:
            out.append(row(d, kind + MONTH_END))
            d = _month_end(d + timedelta(days=1))
    return out


def _month_end(d: date) -> date:
    nxt = date(d.year + d.month // 12, d.month % 12 + 1, 1)
    return nxt - timedelta(days=1)
