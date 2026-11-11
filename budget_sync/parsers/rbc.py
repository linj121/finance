"""RBC Royal Bank: the CSV export from RBC Online Banking (Account Services > Download).
Columns: Account Type, Account Number, Transaction Date, Cheque Number,
         Description 1, Description 2, CAD$, USD$"""
import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ..models import Transaction

CENT = Decimal("0.01")
COLUMNS = {"Account Type", "Account Number", "Transaction Date", "Description 1", "CAD$"}

# Account names match the ones used before (built from statement folders), so transaction
# IDs - and the categories and notes attached to them - carry over.
# Outlook and recurring.py also rely on card accounts containing "Credit".
ACCOUNT_NAMES = {"chequing": "Chequing", "savings": "Saving", "visa": "Credit", "mastercard": "Credit"}


def detect(header: list[str]) -> bool:
    return COLUMNS <= set(header)


def account_name(account_type: str, number: str) -> str:
    kind = ACCOUNT_NAMES.get(account_type.strip().lower(), account_type.strip().title())
    return f"RBC {kind} ...{number.strip()[-4:]}"


def parse(path: Path, source: str) -> list[Transaction]:
    out = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
            if not row.get("Transaction Date"):
                continue
            d = datetime.strptime(row["Transaction Date"], "%m/%d/%Y").date()
            desc = " ".join(filter(None, [row.get("Description 1"), row.get("Description 2")]))
            cad, usd = row.get("CAD$"), row.get("USD$")
            amount, cur = (Decimal(cad), "CAD") if cad else (Decimal(usd), "USD")
            amount = amount.quantize(CENT)   # "1200" -> 1200.00: the amount's text is part of the ID
            out.append(Transaction(d, desc, amount, account_name(row["Account Type"], row["Account Number"]),
                                   source, currency=cur))
    return out
