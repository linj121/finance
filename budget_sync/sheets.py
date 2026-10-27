"""Google Sheets sync. The Transactions, Categories and Balances worksheets are rewritten
on every run; other worksheets (your analysis tabs, Plan) are never touched."""
import gspread

from .models import Balance, Transaction

HEADERS = ["ID", "Date", "Month", "Account", "Description", "Amount", "Currency",
           "Category", "Category Source", "Source File", "Note"]
CATEGORIES_SHEET = "Categories"
BALANCES_SHEET = "Balances"
# A-C are read by the Analytics formulas; E-I by Outlook
BALANCE_HEADERS = ["Account", "As of", "Balance", "Source File", "Kind",
                   "Min Payment", "Due Date", "Credit Limit", "Interest Rate"]


def open_sheet(url: str, key_file: str) -> gspread.Spreadsheet:
    return gspread.service_account(filename=key_file).open_by_url(url)


def _worksheet(sh: gspread.Spreadsheet, title: str, cols: int) -> gspread.Worksheet:
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title, rows=1000, cols=cols)
    if ws.col_count < cols:   # tabs made by an older version have fewer columns
        ws.resize(cols=cols)
    return ws


def read_categories(sh: gspread.Spreadsheet, title: str) -> dict[str, tuple[str, str, str | None]]:
    """(category, category source, note) currently shown in the sheet for each transaction ID.
    note is None when the sheet has no Note column yet."""
    try:
        rows = sh.worksheet(title).get_all_values()
    except gspread.WorksheetNotFound:
        return {}
    if not rows or rows[0][:1] != ["ID"]:
        return {}
    i_id, i_cat, i_src = (rows[0].index(h) for h in ("ID", "Category", "Category Source"))
    i_note = rows[0].index("Note") if "Note" in rows[0] else None
    return {r[i_id]: (r[i_cat].strip(), r[i_src].strip(),
                      None if i_note is None else (r[i_note].strip() if len(r) > i_note else ""))
            for r in rows[1:] if len(r) > i_src and r[i_id]}


def _text(s: str) -> str:
    # Force plain text: under USER_ENTERED, '=…' would become a formula and an ID like
    # '571478363e29' a number. The apostrophe isn't stored in the cell.
    return "'" + s


def push(sh: gspread.Spreadsheet, title: str, txns: list[Transaction], categories: list[str],
         groups: dict[str, str], balances: list[Balance]) -> None:
    cat_ws = _worksheet(sh, CATEGORIES_SHEET, 2)
    cat_ws.clear()
    cat_ws.update([["Category", "Group"]] + [[c, groups.get(c, "")] for c in categories], "A1")

    bal_ws = _worksheet(sh, BALANCES_SHEET, len(BALANCE_HEADERS))
    bal_ws.clear()
    num = lambda v: "" if v is None else float(v)
    bal_ws.update([BALANCE_HEADERS] + [
        [_text(b.account), b.date.isoformat(), float(b.amount), _text(b.source_file), b.kind,
         num(b.min_payment), b.due_date.isoformat() if b.due_date else "", num(b.credit_limit),
         "" if b.rate is None else float(b.rate) / 100]
        for b in sorted(balances, key=lambda b: (b.account, b.date))], "A1", value_input_option="USER_ENTERED")
    sh.batch_update({"requests": [
        {"repeatCell": {"range": {"sheetId": bal_ws.id, "startRowIndex": 1, "startColumnIndex": 1, "endColumnIndex": 2},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}}},
                        "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {"range": {"sheetId": bal_ws.id, "startRowIndex": 1, "startColumnIndex": 2, "endColumnIndex": 3},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00;-$#,##0.00"}}},
                        "fields": "userEnteredFormat.numberFormat"}},
        *[{"repeatCell": {"range": {"sheetId": bal_ws.id, "startRowIndex": 1, "startColumnIndex": c, "endColumnIndex": c + 1},
                          "cell": {"userEnteredFormat": {"numberFormat": fmt}}, "fields": "userEnteredFormat.numberFormat"}}
          for c, fmt in ((5, {"type": "CURRENCY", "pattern": "$#,##0.00"}), (6, {"type": "DATE", "pattern": "yyyy-mm-dd"}),
                         (7, {"type": "CURRENCY", "pattern": "$#,##0.00"}), (8, {"type": "PERCENT", "pattern": "0.00%"}))],
    ]})

    ws = _worksheet(sh, title, len(HEADERS))
    rows = [HEADERS] + [[
        _text(t.id), t.date.isoformat(), t.date.strftime("%Y-%m"), _text(t.account), _text(t.description),
        float(t.amount), t.currency, t.category, t.category_source, _text(t.source_file), _text(t.note) if t.note else "",
    ] for t in txns]
    if ws.row_count < len(rows) + 50:
        ws.resize(rows=len(rows) + 500)
    ws.clear()
    ws.update(rows, "A1", value_input_option="USER_ENTERED")

    n = len(rows)
    sid = ws.id
    sh.batch_update({"requests": [
        {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                                   "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": n,
                                  "startColumnIndex": 1, "endColumnIndex": 2},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}}},
                        "fields": "userEnteredFormat.numberFormat"}},
        # Month is stored as the 1st of the month (a real date, so pivots sort it) but shown as yyyy-mm
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": n,
                                  "startColumnIndex": 2, "endColumnIndex": 3},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm"}}},
                        "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": n,
                                  "startColumnIndex": 5, "endColumnIndex": 6},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00;-$#,##0.00"}}},
                        "fields": "userEnteredFormat.numberFormat"}},
        # Category column gets a dropdown fed from the Categories tab
        {"setDataValidation": {
            "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": n, "startColumnIndex": 7, "endColumnIndex": 8},
            "rule": {"condition": {"type": "ONE_OF_RANGE",
                                   "values": [{"userEnteredValue": f"='{CATEGORIES_SHEET}'!A2:A"}]},
                     "showCustomUi": True, "strict": False}}},
    ]})
