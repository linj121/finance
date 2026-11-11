import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

LEDGER_FIELDS = ["id", "date", "account", "description", "amount", "currency",
                 "category", "category_source", "source_file", "note"]


@dataclass
class Transaction:
    date: date
    description: str
    amount: Decimal          # negative = money out, positive = money in
    account: str
    source_file: str
    currency: str = "CAD"
    category: str = ""
    category_source: str = ""   # rule | memory | ai | manual
    id: str = field(default="")
    note: str = ""              # typed in the sheet; kept across syncs

    def key(self) -> tuple:
        return (self.account, self.date.isoformat(), " ".join(self.description.split()).upper(), str(self.amount))


@dataclass
class Balance:
    """An account's balance on a date (from balances.csv). Debts (credit cards) are negative."""
    account: str
    date: date
    amount: Decimal
    source_file: str
    kind: str = "deposit"                  # deposit | card
    # credit cards only, as printed on the latest statement
    min_payment: Decimal | None = None
    due_date: date | None = None
    credit_limit: Decimal | None = None
    rate: Decimal | None = None            # annual purchase interest rate, %


def assign_ids(txns: list[Transaction]) -> None:
    """Stable IDs: hash of the transaction's fields plus its occurrence number,
    so two identical coffees on the same day stay distinct but re-runs dedupe."""
    seen: Counter = Counter()
    for t in txns:
        k = t.key()
        seen[k] += 1
        raw = "|".join(k) + f"|{seen[k]}"
        t.id = hashlib.sha1(raw.encode()).hexdigest()[:12]
