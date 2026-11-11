"""Usage (from the project folder):
    .venv\\Scripts\\python -m budget_sync run [--no-sync] [--no-ai] [--reclassify]
    .venv\\Scripts\\python -m budget_sync analytics
"""
import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

from .classify import Classifier
from . import balances
from .models import LEDGER_FIELDS, Transaction, assign_ids
from .parsers import parse_file

ROOT = Path(__file__).resolve().parent.parent


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


def collect(data_dir: Path) -> list[Transaction]:
    """Read every transaction CSV under data_dir (subfolders included).
    Downloads may overlap (e.g. a per-account export and RBC's all-accounts export), and the
    same transaction can be worded differently in each ("CONTACTLESS INTERAC PURCHASE - 3597"
    vs "C-IDP PURCHASE-3597"). So transactions are matched on account, date and amount, and
    each is kept as many times as the file that has it most often - not once per file. Two
    identical coffees on one day appear twice in each file and are both kept. Between files
    with the same count, the longer (more detailed) descriptions win."""
    best: dict[tuple, list[Transaction]] = {}
    rank = lambda ts: (len(ts), sum(len(t.description) for t in ts))
    for p in sorted(data_dir.rglob("*.csv")):
        rel = p.relative_to(data_dir).as_posix()
        if rel == balances.FILE_NAME:
            continue
        try:
            got = parse_file(p, rel)
        except Exception as e:  # keep going; one odd file shouldn't block the rest
            print(f"  ! {rel}: {e}")
            continue
        print(f"  {rel}: {len(got)} transactions")
        by_key: dict[tuple, list[Transaction]] = {}
        for t in got:
            by_key.setdefault((t.account, t.date, t.amount), []).append(t)
        for k, ts in by_key.items():
            if k not in best or rank(ts) > rank(best[k]):
                best[k] = ts
    return [t for ts in best.values() for t in ts]


def cmd_run(args, cfg) -> None:
    data_dir = ROOT / cfg.get("data_dir", "data")
    ledger_path = ROOT / cfg.get("output_csv", "transactions.csv")
    print(f"Reading CSV files in {data_dir}")
    txns = collect(data_dir)
    txns.sort(key=lambda t: (t.date, t.account))
    assign_ids(txns)
    bals = balances.roll_forward(balances.load(data_dir / balances.FILE_NAME), txns)
    if not bals:
        print(f"  no balances: add {data_dir / balances.FILE_NAME} for net worth and the Outlook tab")

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
        sheets.push(sh, g.get("worksheet", "Transactions"), txns, clf.categories, clf.groups, bals)
        print(f"Synced to Google Sheets: {g['spreadsheet_url']}")
    elif not args.no_sync:
        print("Google sync skipped: set google.spreadsheet_url in config.yaml")


def cmd_analytics(args, cfg) -> None:
    from . import analytics, sheets
    g = cfg["google"]
    sh = sheets.open_sheet(g["spreadsheet_url"], str(ROOT / g["service_account_file"]))
    clf = Classifier(ROOT / "rules.yaml", ROOT / "category_memory.json", cfg.get("ai", {}))
    if analytics.build(sh, clf.groups):
        print(f"Created the '{analytics.PLAN}' tab with starting budgets: edit it in the sheet")

    # Bills & Goals is pre-filled (once) with recurring items seen in your history, unconfirmed
    from . import outlook, recurring
    rows = sh.worksheet(sheets.BALANCES_SHEET).get_all_records()   # dates shown as yyyy-mm-dd
    latest: dict[str, date] = {}
    for r in rows:
        d = date.fromisoformat(str(r["As of"]))
        latest[r["Account"]] = max(d, latest.get(r["Account"], d))
    deposits = [latest[r["Account"]] for r in rows if r.get("Kind") == "deposit"]
    start = max(deposits) if deposits else date.today()
    complete = min(latest.values()) if latest else date.today()
    month_end = recurring._shift(date(complete.year, complete.month, 1), 1) - timedelta(days=1)
    last_full = recurring._shift(date(complete.year, complete.month, 1), 0 if complete == month_end else -1)
    ledger = list(load_ledger(ROOT / cfg.get("output_csv", "transactions.csv")).values())
    if outlook.build(sh, recurring.suggest(ledger, clf.groups, last_full), start):
        print(f"Created the '{outlook.INPUTS}' tab: confirm the suggested bills there")
    print(f"Rebuilt '{analytics.ANALYTICS}', '{analytics.CHARTS}' and '{outlook.OUTLOOK}' tabs: {g['spreadsheet_url']}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="budget_sync")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="read CSV files, categorize, sync")
    r.add_argument("--no-sync", action="store_true", help="skip Google Sheets")
    r.add_argument("--no-ai", action="store_true", help="don't call Claude for unknown merchants")
    r.add_argument("--overwrite", action="store_true",
                   help="replace the Google Sheet with the local copy, ignoring edits made in the sheet")
    r.add_argument("--reclassify", action="store_true",
                   help="re-apply rules to everything except manual edits (after editing rules.yaml)")
    sub.add_parser("analytics", help="(re)build the Analytics and Charts tabs - discards edits made on them")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    {"run": cmd_run, "analytics": cmd_analytics}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
