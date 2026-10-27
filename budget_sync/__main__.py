"""Usage (from the project folder):
    .venv\\Scripts\\python -m budget_sync run [--no-sync] [--no-ai] [--reclassify]
    .venv\\Scripts\\python -m budget_sync inspect <statement.pdf>
"""
import argparse
import csv
import hashlib
import json
import shutil
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml

from .classify import Classifier
from .models import LEDGER_FIELDS, Balance, Transaction, assign_ids
from .parsers import parse_file

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / ".cache" / "statements.json"


def load_ledger(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as f:
        return {r["id"]: r for r in csv.DictReader(f)}


def save_ledger(path: Path, txns: list[Transaction]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(LEDGER_FIELDS)
        for t in txns:
            w.writerow([t.id, t.date.isoformat(), t.account, t.description, t.amount, t.currency,
                        t.category, t.category_source, t.source_file, t.note])


def _parser_fingerprint() -> str:
    """Changes whenever parser code changes, so a parser fix re-reads every statement."""
    h = hashlib.sha1()
    for p in sorted((ROOT / "budget_sync" / "parsers").glob("*.py")):
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def _to_dict(t: Transaction) -> dict:
    return {"date": t.date.isoformat(), "description": t.description, "amount": str(t.amount),
            "account": t.account, "source_file": t.source_file, "currency": t.currency}


def _from_dict(d: dict) -> Transaction:
    return Transaction(date.fromisoformat(d["date"]), d["description"], Decimal(d["amount"]),
                       d["account"], d["source_file"], currency=d["currency"])


def _balance_to_dict(b: Balance | None) -> dict | None:
    return b and {"account": b.account, "date": b.date.isoformat(), "amount": str(b.amount),
                  "source_file": b.source_file}


def _balance_from_dict(d: dict | None) -> Balance | None:
    return d and Balance(d["account"], date.fromisoformat(d["date"]), Decimal(d["amount"]), d["source_file"])


def collect(statements_dir: Path, use_cache: bool = True) -> tuple[list[Transaction], list[Balance]]:
    """Parse every statement, reusing cached results for files that haven't changed
    (same size and modified time, same parser code). Failed files are never cached.
    Returns the transactions and each statement's closing balance."""
    files = sorted(p for p in statements_dir.rglob("*") if p.suffix.lower() in (".pdf", ".csv"))
    key = {"parser": _parser_fingerprint(), "dir": str(statements_dir)}
    cache = {}
    if use_cache and CACHE_PATH.exists():
        saved = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if all(saved.get(k) == v for k, v in key.items()):
            cache = saved["files"]

    txns: list[Transaction] = []
    balances: list[Balance] = []
    fresh: dict[str, dict] = {}
    reused = 0
    for p in files:
        rel = str(p.relative_to(statements_dir))
        st = p.stat()
        sig = [st.st_size, st.st_mtime_ns]
        hit = cache.get(rel)
        if hit and hit["sig"] == sig:
            got = [_from_dict(d) for d in hit["transactions"]]
            bal = _balance_from_dict(hit.get("balance"))
            reused += 1
        else:
            try:
                st = parse_file(p)
            except Exception as e:  # keep going; one odd file shouldn't block the rest
                print(f"  ! {rel}: {e}")
                continue
            got, bal = st.transactions, st.balance
            print(f"  {rel}: {len(got)} transactions")
        fresh[rel] = {"sig": sig, "transactions": [_to_dict(t) for t in got], "balance": _balance_to_dict(bal)}
        txns.extend(got)
        if bal:
            balances.append(bal)

    CACHE_PATH.parent.mkdir(exist_ok=True)
    CACHE_PATH.write_text(json.dumps({**key, "files": fresh}, ensure_ascii=False), encoding="utf-8")
    if reused:
        print(f"  {reused} unchanged statements loaded from cache")
    return txns, balances


def cmd_run(args, cfg) -> None:
    statements_dir = Path(cfg["statements_dir"])
    ledger_path = ROOT / cfg.get("output_csv", "transactions.csv")
    print(f"Reading statements in {statements_dir}")
    if args.force:
        args.reclassify = True   # --force: re-read every statement and re-categorize every record
    txns, balances = collect(statements_dir, use_cache=not args.force)
    txns.sort(key=lambda t: (t.date, t.account))
    assign_ids(txns)
    # the same statement saved twice would otherwise double-count
    txns = list({t.id: t for t in txns}.values())

    clf = Classifier(ROOT / "rules.yaml", ROOT / "category_memory.json", cfg.get("ai", {}))

    # 1. keep categories already decided (stable across runs)
    ledger = load_ledger(ledger_path)
    for t in txns:
        old = ledger.get(t.id)
        if old and (old["category_source"] == "manual" or not args.reclassify):
            t.category, t.category_source = old["category"], old["category_source"]
        if old:
            t.note = old.get("note") or ""

    # 2. pick up edits you made in Google Sheets
    g = cfg.get("google") or {}
    sh = None
    if not args.no_sync and g.get("spreadsheet_url"):
        from . import sheets
        sh = sheets.open_sheet(g["spreadsheet_url"], str(ROOT / g["service_account_file"]))
        # --overwrite: the local copy wins; changes made in the sheet are discarded
        remote = {} if args.overwrite else sheets.read_categories(sh, g.get("worksheet", "Transactions"))
        edits = 0
        for t in txns:
            cat, src, note = remote.get(t.id, ("", "", None))
            if note is not None:   # the sheet is where notes are written
                t.note = note
            last_pushed = ledger.get(t.id, {}).get("category")
            # an edit = the cell no longer shows what we last wrote there
            edited = src == "manual" or (last_pushed is not None and cat != last_pushed)
            if edited and cat in clf.categories and cat != "Uncategorized":
                t.category, t.category_source = cat, "manual"
                clf.remember(t.description, cat, "manual")
                edits += 1
        if edits:
            print(f"  picked up {edits} category edits from Google Sheets")

    # 3. categorize everything still open
    pending = [t for t in txns if not t.category or t.category == "Uncategorized" or args.reclassify]
    use_ai = (cfg.get("ai") or {}).get("enabled", True) and not args.no_ai
    clf.classify(pending, use_ai=use_ai)
    clf.save()
    save_ledger(ledger_path, txns)

    uncategorized = sum(t.category == "Uncategorized" for t in txns)
    print(f"{len(txns)} transactions -> {ledger_path} ({uncategorized} uncategorized)")

    if sh is not None:
        sheets.push(sh, g.get("worksheet", "Transactions"), txns, clf.categories, clf.groups, balances)
        print(f"Synced to Google Sheets: {g['spreadsheet_url']}")
    elif not args.no_sync:
        print("Google sync skipped: set google.spreadsheet_url in config.yaml")


def cmd_inspect(args, cfg) -> None:
    """Dump what pdfplumber sees - used to build/fix a parser for a new statement layout."""
    import pdfplumber
    with pdfplumber.open(args.file) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            print(f"===== page {i} =====")
            print(page.extract_text())
    try:
        st = parse_file(Path(args.file))
        got = st.transactions
        print(f"\n===== parsed {len(got)} transactions; closing balance: {st.balance} =====")
        for t in got:
            print(f"{t.date}  {t.amount:>10}  {t.account:<22} {t.description}")
    except Exception as e:
        print(f"\nparser failed: {e}")


def cmd_clear_cache(args, cfg) -> None:
    cache_dir = CACHE_PATH.parent
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
        print(f"Deleted {cache_dir}; the next run re-reads every statement.")
    else:
        print("No cache to clear.")


def cmd_analytics(args, cfg) -> None:
    from . import analytics, sheets
    g = cfg["google"]
    sh = sheets.open_sheet(g["spreadsheet_url"], str(ROOT / g["service_account_file"]))
    clf = Classifier(ROOT / "rules.yaml", ROOT / "category_memory.json", cfg.get("ai", {}))
    if analytics.build(sh, clf.groups):
        print(f"Created the '{analytics.PLAN}' tab with starting budgets: edit it in the sheet")
    print(f"Rebuilt '{analytics.ANALYTICS}' and '{analytics.CHARTS}' tabs: {g['spreadsheet_url']}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="budget_sync")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="parse statements, categorize, sync")
    r.add_argument("--no-sync", action="store_true", help="skip Google Sheets")
    r.add_argument("--no-ai", action="store_true", help="don't call Claude for unknown merchants")
    r.add_argument("--force", action="store_true",
                   help="ignore the cache: re-read every statement and re-categorize every record "
                        "(manual edits are kept)")
    r.add_argument("--overwrite", action="store_true",
                   help="replace the Google Sheet with the local copy, ignoring edits made in the sheet")
    r.add_argument("--reclassify", action="store_true",
                   help="re-apply rules to everything except manual edits (after editing rules.yaml)")
    i = sub.add_parser("inspect", help="show a statement's raw text and parse result")
    i.add_argument("file")
    sub.add_parser("analytics", help="(re)build the Analytics and Charts tabs - discards edits made on them")
    sub.add_parser("clear-cache", help="delete cached statement parses so the next run re-reads everything")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    {"run": cmd_run, "inspect": cmd_inspect, "analytics": cmd_analytics,
     "clear-cache": cmd_clear_cache}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
