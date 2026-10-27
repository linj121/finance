"""RBC Royal Bank: chequing/savings PDF statements, credit card PDF statements,
and the CSV export from RBC Online Banking (Account Services > Download)."""
import csv
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pdfplumber

from ..models import Balance, Statement, Transaction

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
MONEY_RE = re.compile(r"^-?\$?-?[\d,]+\.\d{2}$")
CSV_HEADER = "Account Type"


def detect(path: Path, text: str) -> bool:
    if path.suffix.lower() == ".csv":
        return text.lstrip().strip('"').startswith(CSV_HEADER)
    t = text.upper().replace(" ", "")
    return "ROYALBANK" in t or "RBC" in t


def parse(path: Path) -> Statement:
    if path.suffix.lower() == ".csv":
        return Statement(_parse_csv(path))
    with pdfplumber.open(path) as pdf:
        text = "\n".join((p.extract_text(x_tolerance=X_TOL) or "") for p in pdf.pages)
        if re.search(r"TRANSACTION\s+POSTING", text.upper()):
            return _with_balance(_parse_card(pdf, text, path), text, path, "card")
        return _with_balance(_parse_deposit(pdf, text, path), text, path, "chequing")


AMOUNT = r"(-?\$?-?[\d,]+\.\d{2})"
BALANCES = {   # (opening, closing) as printed on each kind of statement
    "card": (re.compile(r"PREVIOUS ACCOUNT BALANCE\s*" + AMOUNT, re.I),
             re.compile(r"TOTAL ACCOUNT BALANCE\s*" + AMOUNT, re.I)),
    "chequing": (re.compile(r"Opening\s*balance[^\n]*?" + AMOUNT, re.I),
                 re.compile(r"Closing\s*balance[^\n]*?" + AMOUNT, re.I)),
}


def _with_balance(txns: list[Transaction], text: str, path: Path, kind: str) -> Statement:
    """Attach the closing balance, after checking opening + transactions = closing,
    so a skipped or misread row can't slip through."""
    o, c = (rx.search(text) for rx in BALANCES[kind])
    if not (o and c):
        return Statement(txns)
    opening, closing = _money(o[1]), _money(c[1])
    if kind == "card":   # the card prints what you owe; store it as a negative balance
        opening, closing = -opening, -closing
    flow = sum((t.amount for t in txns), Decimal(0))
    if opening + flow != closing:
        raise ValueError(f"balance mismatch: opening {opening} + transactions {flow} != closing {closing}")
    m = (CARD_PERIOD if kind == "card" else DEP_PERIOD).search(text)
    end = date(int(m[6]), MONTHS[m[4][:3].upper()], int(m[5]))
    account = txns[0].account if txns else _account_label(text, kind, path)
    return Statement(txns, Balance(account, end, closing, path.name))


def _money(s: str) -> Decimal:
    neg = "-" in s
    v = Decimal(s.replace("$", "").replace(",", "").replace("-", ""))
    return -v if neg else v


def _resolve_year(month: int, day: int, start: date, end: date) -> date:
    """Statements give 'Jan 03' without a year; pick the year that falls in the period."""
    for y in (end.year, end.year - 1):
        d = date(y, month, day)
        if start - timedelta(days=7) <= d <= end + timedelta(days=7):
            return d
    return date(end.year, month, day)


def _account_label(text: str, kind: str, path: Path) -> str:
    if kind == "card":
        # "4510 14** **** 3503" or "XXXX XXXX 1234"
        m = re.search(r"\d{4}\s+[\d*]{2,4}\*+\s*\*{0,4}\s*(\d{4})|X{4}\s+X{4}\s+(\d{4})", text)
    else:
        m = re.search(r"account\s*number:?\s*[\d-]*(\d{4})\b", text, re.I)
    tail = next((g for g in (m.groups() if m else ()) if g), None)
    folder = path.parent.name
    name = f"RBC {folder.title()}" if folder.lower() != "rbc" else f"RBC {kind.title()}"
    return f"{name} ...{tail}" if tail else name


# ---------- credit card ----------
CARD_LINE = re.compile(
    r"^(?P<tm>[A-Z]{3})\s?(?P<td>\d{1,2})\s+(?P<pm>[A-Z]{3})\s?(?P<pd>\d{1,2})\s+"
    r"(?P<desc>.+?)\s+(?P<amt>-?\$[\d,]+\.\d{2})$")
CARD_PERIOD = re.compile(
    r"STATEMENT FROM\s+([A-Z]{3})\s+(\d{1,2}),?\s*(\d{4})?\s+TO\s+([A-Z]{3})\s+(\d{1,2}),?\s*(\d{4})", re.I)
# "Calculating your balance" box: credits are printed negative, the rest are charges
CARD_TOTAL = re.compile(
    r"(Payments & credits|Purchases & debits|Cash advances|Interest|Fees)\s*(-?\$[\d,]+\.\d{2})", re.I)
TABLE_GUTTER = 25   # the sidebar (Contact us, Payments & interest rates) starts ~45pt right of AMOUNT


def _parse_card(pdf, text: str, path: Path) -> list[Transaction]:
    m = CARD_PERIOD.search(text)
    if not m:
        raise ValueError("could not find 'STATEMENT FROM ... TO ...' period")
    sm, sd, sy, em, ed, ey = m.groups()
    end = date(int(ey), MONTHS[em.upper()], int(ed))
    start = date(int(sy) if sy else end.year, MONTHS[sm.upper()], int(sd))
    if start > end:
        start = start.replace(year=start.year - 1)
    account = _account_label(text, "card", path)
    out = []
    for line in _card_table_text(pdf).splitlines():
        lm = CARD_LINE.match(line.strip().upper())
        if not lm or lm["tm"] not in MONTHS or lm["pm"] not in MONTHS:
            continue
        # the posting date sits inside the period; the transaction date can be weeks earlier (refunds)
        posted = _resolve_year(MONTHS[lm["pm"]], int(lm["pd"]), start, end)
        d = date(posted.year, MONTHS[lm["tm"]], int(lm["td"]))
        if d > posted:
            d = d.replace(year=d.year - 1)
        # On a card statement a positive amount is a charge (money out).
        out.append(Transaction(d, lm["desc"].strip(), -_money(lm["amt"]), account, path.name))
    return _check_card_totals(out, text)


def _card_table_text(pdf) -> str:
    """Text of the activity table only. The right-hand sidebar shares lines with the
    transactions ("... $260.29 Customer Service 1-800-..."), so crop it off."""
    right = None
    pages = []
    for page in pdf.pages:
        head = next((w for w in page.extract_words(x_tolerance=X_TOL) if w["text"] == "AMOUNT"), None)
        if head:
            right = head["x1"] + TABLE_GUTTER
        if right:   # continuation pages without a header reuse the last one's width
            pages.append(page.crop((0, 0, min(right, page.width), page.height)).extract_text(x_tolerance=X_TOL) or "")
    return "\n".join(pages)


def _check_card_totals(txns: list[Transaction], text: str) -> list[Transaction]:
    totals: dict[str, Decimal] = {}
    for label, amt in CARD_TOTAL.findall(text):
        totals.setdefault(label.lower(), _money(amt))   # first hit is the summary box
    if not totals:
        return txns
    want_credits = -totals.get("payments & credits", Decimal(0))
    want_charges = sum((v for k, v in totals.items() if k != "payments & credits"), Decimal(0))
    got_credits = sum((t.amount for t in txns if t.amount > 0), Decimal(0))
    got_charges = -sum((t.amount for t in txns if t.amount < 0), Decimal(0))
    if (got_credits, got_charges) != (want_credits, want_charges):
        raise ValueError(f"totals mismatch: parsed credits {got_credits} / charges {got_charges}, "
                         f"statement says credits {want_credits} / charges {want_charges}")
    return txns


# ---------- chequing / savings ----------
# RBC's text layer packs characters tightly; a small x_tolerance keeps word gaps.
X_TOL = 1.5
DEP_PERIOD = re.compile(
    r"From\s*([A-Za-z]+)\s*(\d{1,2}),\s*(\d{4})\s*to\s*([A-Za-z]+)\s*(\d{1,2}),\s*(\d{4})")
DEP_TOTAL = re.compile(r"Total\s*(deposits|withdrawals)\s*(?:into|from)\s*your\s*account\s*[+-]?\s*\$?([\d,]+\.\d{2})", re.I)
DATE_TOKEN = re.compile(r"^(\d{1,2})$")
DATE_COL_MAX_X = 85   # date column ends before the description starts (x≈90)
LEFT_MARGIN_X = 40    # printer barcode/serial strings live at x≈15
# printer artifacts: *000000K3*, 4169540-390_8593884_02001ADPBR
PRINT_CODE = re.compile(r"^(\*0+[0-9A-Z]+\*|[-\d]*_\d+_\d+[A-Z]+)$")
PAGE_MARK = re.compile(r"^\d+ of \d+$")   # "1 of 6" page counter


def _parse_deposit(pdf, text: str, path: Path) -> list[Transaction]:
    m = DEP_PERIOD.search(text)
    if not m:
        raise ValueError("could not find 'From <date> to <date>' statement period")
    sm, sd, sy, em, ed, ey = m.groups()
    start = date(int(sy), MONTHS[sm[:3].upper()], int(sd))
    end = date(int(ey), MONTHS[em[:3].upper()], int(ed))
    account = _account_label(text, "chequing", path)

    out: list[Transaction] = []
    cur_date: date | None = None
    pending: list[str] = []
    balance: Decimal | None = None
    first_break = None   # first row where running balance != printed balance
    for page in pdf.pages:
        words = [w for w in page.extract_words(x_tolerance=X_TOL)
                 if w["x0"] >= LEFT_MARGIN_X and not PRINT_CODE.match(w["text"])]
        cols = _column_edges(words)
        if not cols:
            continue
        header_top = cols.pop("_top")
        amount_min_x = cols.pop("_amount_min_x")
        for line in _lines(w for w in words if w["top"] > header_top + 2):
            # Leading "16 Sep" sets the date for this and following rows.
            if (len(line) >= 2 and line[0]["x1"] < DATE_COL_MAX_X and DATE_TOKEN.match(line[0]["text"])
                    and line[1]["text"][:3].upper() in MONTHS):
                cur_date = _resolve_year(MONTHS[line[1]["text"][:3].upper()], int(line[0]["text"]), start, end)
                line = line[2:]
            # only numbers sitting in the amount columns count (not "1@$5.00" inside a description)
            amounts = [w for w in line if MONEY_RE.match(w["text"]) and w["x0"] >= amount_min_x]
            desc = " ".join(w["text"] for w in line if w not in amounts).strip()
            key = desc.replace(" ", "").upper()
            if key.startswith("CLOSINGBALANCE"):
                return _check_totals(out, text, first_break)
            if key.startswith("OPENINGBALANCE"):
                if amounts:
                    balance = _money(amounts[-1]["text"])
                continue
            if not amounts:
                if desc and not PAGE_MARK.match(desc):
                    pending.append(desc)
                continue
            full = " ".join(pending + [desc]).strip()
            pending = []
            if not full or cur_date is None:
                continue
            for w in amounts:
                col = min(cols, key=lambda c: abs(cols[c] - w["x1"]))
                if col == "withdrawals":
                    out.append(Transaction(cur_date, full, -_money(w["text"]), account, path.name))
                elif col == "deposits":
                    out.append(Transaction(cur_date, full, _money(w["text"]), account, path.name))
                if balance is not None and col != "balance":
                    balance += out[-1].amount
                elif col == "balance" and balance is not None:
                    if balance != _money(w["text"]) and first_break is None:
                        first_break = f"{cur_date} '{full}' (running {balance}, printed {w['text']})"
                    balance = _money(w["text"])
    return _check_totals(out, text, first_break)


def _check_totals(txns: list[Transaction], text: str, first_break: str | None) -> list[Transaction]:
    """Compare against the statement's own summary box; a mismatch means a misread row."""
    totals = {k.lower(): Decimal(v.replace(",", "")) for k, v in DEP_TOTAL.findall(text)}
    got_in = sum((t.amount for t in txns if t.amount > 0), Decimal(0))
    got_out = -sum((t.amount for t in txns if t.amount < 0), Decimal(0))
    if totals and (totals.get("deposits") != got_in or totals.get("withdrawals") != got_out):
        raise ValueError(f"totals mismatch: parsed in {got_in} / out {got_out}, statement says "
                         f"in {totals.get('deposits')} / out {totals.get('withdrawals')}"
                         + (f"; balance first off at {first_break}" if first_break else ""))
    return txns


def _column_edges(words) -> dict | None:
    """Right edge (x1) of each amount column header, used to tell a withdrawal from a deposit.
    Matches the table header row only (capitalised, all three on one line), not the summary box."""
    for head in (w for w in words if w["text"].startswith("Withdrawals")):
        row = [w for w in words if abs(w["top"] - head["top"]) < 2]
        found = {"withdrawals": head}
        for key, label in (("deposits", "Deposits"), ("balance", "Balance")):
            found.update({key: w for w in row if w["text"].startswith(label)})
        if len(found) == 3:
            # amounts are right-aligned under headers like "Withdrawals ($)"
            edges = {k: _header_right(words, w) for k, w in found.items()}
            edges["_top"] = head["top"]
            edges["_amount_min_x"] = head["x0"] - 30
            return edges
    return None


def _header_right(words, head) -> float:
    same_line = [w for w in words if abs(w["top"] - head["top"]) < 2 and w["x0"] >= head["x0"]
                 and w["x0"] - head["x1"] < 15]
    return max([head["x1"]] + [w["x1"] for w in same_line if w["text"].startswith("(")])


def _lines(words, tol: float = 3.0):
    rows: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if rows and abs(rows[-1][0]["top"] - w["top"]) <= tol:
            rows[-1].append(w)
        else:
            rows.append([w])
    return [sorted(r, key=lambda w: w["x0"]) for r in rows]


# ---------- CSV export ----------
def _parse_csv(path: Path) -> list[Transaction]:
    out = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("Transaction Date"):
                continue
            d = datetime.strptime(row["Transaction Date"].strip(), "%m/%d/%Y").date()
            desc = " ".join(filter(None, [row.get("Description 1", "").strip(),
                                          row.get("Description 2", "").strip()]))
            cad, usd = row.get("CAD$", "").strip(), row.get("USD$", "").strip()
            amount, cur = (Decimal(cad), "CAD") if cad else (Decimal(usd), "USD")
            acct = f"RBC {row['Account Type'].strip()} ...{row['Account Number'].strip()[-4:]}"
            out.append(Transaction(d, desc, amount, acct, path.name, currency=cur))
    return out
