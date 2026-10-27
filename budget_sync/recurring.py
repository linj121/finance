"""Suggest recurring income and bills from transaction history. These are only
suggestions: they go into the Bills & Goals tab unconfirmed, and nothing counts in the
cash-flow forecast until you tick them."""
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .classify import normalize

SKIP_GROUPS = {"Not spending"}      # transfers, card payments, paid-for-others
KEEP_ANYWAY = {"Income"}            # ...except income itself
# money out is only suggested from bill-like categories (not the café you visit often)
BILL_GROUPS = {"Fixed"}
BILL_CATEGORIES = {"Utilities", "Fees & Interest", "Health", "Education"}
MONTHS_LOOKED_AT = 6
MIN_MONTHS = 4                      # seen in at least this many of those months
MAX_SPREAD = Decimal("0.25")        # amounts within ±25% of the typical one


@dataclass
class Suggestion:
    name: str
    kind: str          # Income | Bill
    amount: Decimal    # positive
    day: int           # usual day of the month
    paid_from: str     # Cash | Card
    category: str
    months_seen: int


def _key(desc: str) -> str:
    k = normalize(desc)
    k = re.sub(r"^(MISC PAYMENT|ONLINE BANKING PAYMENT|BILL PAYMENT|E-TRANSFER - AUTODEPOSIT|"
               r"E-TRANSFER SENT|PREAUTHORIZED DEBIT|DEPOSIT)\s*-?\s*", "", k)
    return " ".join(k.split()[:3])


def suggest(rows: list[dict], groups: dict[str, str], last_month: date) -> list[Suggestion]:
    """rows: ledger rows (date, account, description, amount, category).
    last_month: first day of the latest complete month."""
    months = [_shift(last_month, -i) for i in range(MONTHS_LOOKED_AT)]
    seen: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        d = date.fromisoformat(r["date"])
        cat = r["category"]
        if date(d.year, d.month, 1) not in months:
            continue
        if groups.get(cat) in SKIP_GROUPS and cat not in KEEP_ANYWAY:
            continue
        amt = Decimal(r["amount"])
        if amt < 0 and groups.get(cat) not in BILL_GROUPS and cat not in BILL_CATEGORIES:
            continue
        seen[(_key(r["description"]), amt > 0, cat)].append({"d": d, "amt": abs(amt), "cat": cat,
                                                             "card": "Credit" in r["account"]})
    out = []
    for (key, is_in, cat), hits in seen.items():
        per_month = defaultdict(list)
        for h in hits:
            per_month[(h["d"].year, h["d"].month)].append(h)
        if len(per_month) < MIN_MONTHS:
            continue
        # twice a month in most months (e.g. semi-monthly pay): one suggestion per half of the month
        twice = sum(len(v) >= 2 for v in per_month.values()) > len(per_month) / 2
        halves = [hits] if not twice else [[h for h in hits if h["d"].day <= 15],
                                            [h for h in hits if h["d"].day > 15]]
        for part in halves:
            if len({(h["d"].year, h["d"].month) for h in part}) < (MIN_MONTHS if not twice else 2):
                continue
            # judge by the latest three: pay raises and rent changes shouldn't hide a regular item
            recent = sorted(h["amt"] for h in sorted(part, key=lambda h: h["d"])[-3:])
            typical = recent[len(recent) // 2]
            if any(abs(a - typical) > typical * MAX_SPREAD for a in recent):
                continue   # amounts too irregular to plan around
            out.append(Suggestion(key.title(), "Income" if is_in else "Bill", typical,
                                  _usual_day(h["d"].day for h in part),
                                  "Card" if statistics.mode(h["card"] for h in part) else "Cash",
                                  cat, len({(h["d"].year, h["d"].month) for h in part})))
    return sorted(out, key=lambda s: (s.kind != "Income", -s.amount))


def _usual_day(days) -> int:
    """Median day of the month, treating the last week as just before the 1st, so rent paid
    on the 29th one month and the 2nd the next comes out near month-end, not the 15th."""
    m = statistics.median(d - 31 if d > 24 else d for d in days)
    return max(1, round(m)) if m >= 0.5 else min(31, round(31 + m))


def _shift(d: date, months: int) -> date:
    m = d.month - 1 + months
    return date(d.year + m // 12, m % 12 + 1, 1)
