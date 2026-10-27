"""Builds the Analytics and Charts tabs. Everything is a live formula over the
Transactions tab, so the numbers refresh after every sync; rebuilding these two
tabs only matters when this layout changes."""
import gspread

ANALYTICS = "Analytics"
CHARTS = "Charts"
NON_SPENDING = ["Income", "Transfer", "Credit Card Payment", "Investments"]

# validated categorical slots 1-2 and blue sequential ramp (dataviz reference palette)
BLUE, ORANGE = "#2a78d6", "#eb6834"
RAMP_LOW, RAMP_HIGH = "#fcfcfb", "#6da7ec"
INK_MUTED = "#52514e"

# shorthands expanded in the formulas below
SUBS = {
    "DATE": "Transactions!$B$2:$B", "MONTH": "Transactions!$C$2:$C", "DESC": "Transactions!$E$2:$E",
    "AMT": "Transactions!$F$2:$F", "CAT": "Transactions!$H$2:$H",
    "INPERIOD": "Transactions!$B$2:$B>=$B$4, Transactions!$B$2:$B<=$B$5",
    "SPENDCAT": "(Transactions!$H$2:$H<>\"\")*ISNA(MATCH(Transactions!$H$2:$H,$E$4:$E$7,0))",
}


def f(formula: str) -> str:
    for k, v in SUBS.items():
        formula = formula.replace("{" + k + "}", v)
    return formula


CELLS = {
    "A1": "Budget analytics",
    "A2": "Live formulas over the Transactions tab. Change the period in B4:B5, or the "
          "non-spending categories in E4:E7.",
    "A4": "From", "B4": f("=MIN({DATE})"),
    "A5": "To", "B5": f("=MAX({DATE})"),
    "D4": "Not spending:",
    **{f"E{4 + i}": c for i, c in enumerate(NON_SPENDING)},

    # headline numbers for the period
    "A8": "Income", "B8": "Spending", "C8": "Net", "D8": "Savings rate", "E8": "Monthly avg", "F8": "Months",
    "A9": f('=SUMIFS({AMT}, {CAT}, "Income", {DATE}, ">="&$B$4, {DATE}, "<="&$B$5)'),
    "B9": f("=-SUMPRODUCT(({DATE}>=$B$4)*({DATE}<=$B$5)*{SPENDCAT}, {AMT})"),
    "C9": "=A9-B9",
    "D9": "=IF(A9=0,,C9/A9)",
    "E9": "=IF(F9=0,,B9/F9)",
    "F9": f("=IFERROR(COUNTUNIQUE(FILTER({MONTH}, {INPERIOD})),0)"),

    # month by month
    "A11": "By month",
    "A12": "Month", "B12": "Income", "C12": "Spending", "D12": "Net", "E12": "Savings rate",
    "A13": f('=IFERROR(SORT(UNIQUE(FILTER({MONTH}, {MONTH}<>"", {INPERIOD}))))'),
    "B13": f('=MAP(A13:A, LAMBDA(m, IF(m="",, SUMIFS({AMT}, {MONTH}, m, {CAT}, "Income", '
             '{DATE}, ">="&$B$4, {DATE}, "<="&$B$5))))'),
    "C13": f('=MAP(A13:A, LAMBDA(m, IF(m="",, -SUMPRODUCT(({MONTH}=m)*({DATE}>=$B$4)*({DATE}<=$B$5)*{SPENDCAT}, {AMT}))))'),
    "D13": '=MAP(A13:A, B13:B, C13:C, LAMBDA(m, i, s, IF(m="",, i-s)))',
    "E13": '=MAP(A13:A, B13:B, D13:D, LAMBDA(m, i, n, IF(OR(m="", i=0),, n/i)))',

    # by category, biggest first
    "H11": "By category",
    "H12": "Category", "I12": "Spent", "J12": "Share", "K12": "Avg / month",
    "L12": f('=IFERROR("In "&TEXT(MAX(FILTER({MONTH}, {INPERIOD})), "mmm yyyy"), "Latest month")'),
    "H13": f('=LET(cats, FILTER(Categories!$A$2:$A, Categories!$A$2:$A<>"", ISNA(MATCH(Categories!$A$2:$A, $E$4:$E$7, 0))), '
             'spent, MAP(cats, LAMBDA(c, -SUMIFS({AMT}, {CAT}, c, {DATE}, ">="&$B$4, {DATE}, "<="&$B$5))), '
             'lastm, MAX(FILTER({MONTH}, {INPERIOD})), '
             'latest, MAP(cats, LAMBDA(c, -SUMIFS({AMT}, {CAT}, c, {MONTH}, lastm))), '
             'total, SUM(spent), '
             'share, MAP(spent, LAMBDA(s, IF(total=0, 0, s/total))), '
             'avg, MAP(spent, LAMBDA(s, IF($F$9=0, 0, s/$F$9))), '
             'tbl, HSTACK(cats, spent, share, avg, latest), '
             'IFERROR(SORT(FILTER(tbl, spent<>0), 2, FALSE), "No spending in this period"))'),

    # biggest single expenses
    "N11": "Largest expenses",
    "N12": "Date", "O12": "Description", "P12": "Category", "Q12": "Amount",
    "N13": f("=IFERROR(SORTN(FILTER(HSTACK({DATE}, {DESC}, {CAT}, -{AMT}), {AMT}<0, {INPERIOD}, "
             "ISNA(MATCH({CAT},$E$4:$E$7,0))), 15, 0, 4, FALSE))"),

    # heat table: category x last 12 months, in its own columns so widths stay even
    "S11": "Spending by category, last 12 months",
    "S12": "Category",
    "T12": f('=IFERROR(TRANSPOSE(SORT(SORTN(UNIQUE(FILTER({MONTH}, {MONTH}<>"", {INPERIOD})), 12, 0, 1, FALSE))))'),
    "S13": '=IFERROR(FILTER(H13:H39, H13:H39<>""))',
    "T13": f("=IFERROR(MAKEARRAY(COUNTA(S13:S), COUNTA(T12:AE12), LAMBDA(r, c, "
             "-SUMIFS({AMT}, {CAT}, INDEX(S13:S, r), {MONTH}, INDEX(T12:AE12, 1, c)))))"),
}

MONEY = {"type": "CURRENCY", "pattern": "$#,##0;-$#,##0"}
PCT = {"type": "PERCENT", "pattern": "0%"}
MONTH_FMT = {"type": "DATE", "pattern": "yyyy-mm"}
DAY_FMT = {"type": "DATE", "pattern": "yyyy-mm-dd"}


def _grid(sid, a1: str) -> dict:
    """'B13:E' -> GridRange (open-ended rows allowed)."""
    def split(ref):
        col = "".join(ch for ch in ref if ch.isalpha())
        row = "".join(ch for ch in ref if ch.isdigit())
        c = 0
        for ch in col:
            c = c * 26 + ord(ch.upper()) - 64
        return c - 1, (int(row) if row else None)
    start, _, end = a1.partition(":")
    c0, r0 = split(start)
    c1, r1 = split(end or start)
    g = {"sheetId": sid, "startColumnIndex": c0, "endColumnIndex": c1 + 1, "startRowIndex": r0 - 1}
    if r1 is not None:
        g["endRowIndex"] = r1
    elif not end:
        g["endRowIndex"] = r0
    return g


def _fmt(sid, a1, fmt, fields):
    return {"repeatCell": {"range": _grid(sid, a1), "cell": {"userEnteredFormat": fmt},
                           "fields": "userEnteredFormat(" + fields + ")"}}


def _rgb(hexcolor: str) -> dict:
    h = hexcolor.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


def _recreate(sh: gspread.Spreadsheet, title: str, index: int) -> gspread.Worksheet:
    try:
        sh.del_worksheet(sh.worksheet(title))
    except gspread.WorksheetNotFound:
        pass
    return sh.add_worksheet(title, rows=200, cols=32, index=index)


def build(sh: gspread.Spreadsheet) -> None:
    ws = _recreate(sh, ANALYTICS, 0)
    charts = _recreate(sh, CHARTS, 1)
    ws.batch_update([{"range": k, "values": [[v]]} for k, v in CELLS.items()], value_input_option="USER_ENTERED")

    a, c = ws.id, charts.id
    bold = {"textFormat": {"bold": True}}
    reqs = [
        _fmt(a, "A1", {"textFormat": {"bold": True, "fontSize": 16}}, "textFormat"),
        _fmt(a, "A2", {"textFormat": {"italic": True, "foregroundColor": _rgb(INK_MUTED)}}, "textFormat"),
        *[_fmt(a, r, bold, "textFormat") for r in ("A4:A5", "D4", "A8:F8", "A11", "H11", "N11", "S11",
                                                    "A12:E12", "H12:L12", "N12:Q12", "S12:AE12")],
        _fmt(a, "A9:F9", {"textFormat": {"bold": True, "fontSize": 14}}, "textFormat"),
        _fmt(a, "B4:B5", {"numberFormat": DAY_FMT, "backgroundColor": _rgb("#fff8e1")}, "numberFormat,backgroundColor"),
        _fmt(a, "E4:E7", {"backgroundColor": _rgb("#fff8e1")}, "backgroundColor"),
        _fmt(a, "A9:C9", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, "E9", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, "D9", {"numberFormat": PCT}, "numberFormat"),
        _fmt(a, "A13:A", {"numberFormat": MONTH_FMT, "horizontalAlignment": "LEFT"}, "numberFormat,horizontalAlignment"),
        _fmt(a, "B13:D", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, "E13:E", {"numberFormat": PCT}, "numberFormat"),
        _fmt(a, "I13:I39", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, "J13:J39", {"numberFormat": PCT}, "numberFormat"),
        _fmt(a, "K13:L39", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, "N13:N27", {"numberFormat": DAY_FMT}, "numberFormat"),
        _fmt(a, "Q13:Q27", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, "T12:AE12", {"numberFormat": MONTH_FMT, "horizontalAlignment": "RIGHT"}, "numberFormat,horizontalAlignment"),
        _fmt(a, "T13:AE", {"numberFormat": MONEY}, "numberFormat"),
        {"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [_grid(a, "T13:AE40")],
            "gradientRule": {"minpoint": {"color": _rgb(RAMP_LOW), "type": "NUMBER", "value": "0"},
                             "maxpoint": {"color": _rgb(RAMP_HIGH), "type": "MAX"}}}}},
        {"updateDimensionProperties": {"range": {"sheetId": a, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 6},
                                       "properties": {"pixelSize": 120}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": a, "dimension": "COLUMNS", "startIndex": 6, "endIndex": 7},
                                       "properties": {"pixelSize": 30}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": a, "dimension": "COLUMNS", "startIndex": 7, "endIndex": 8},
                                       "properties": {"pixelSize": 160}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": a, "dimension": "COLUMNS", "startIndex": 12, "endIndex": 13},
                                       "properties": {"pixelSize": 30}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": a, "dimension": "COLUMNS", "startIndex": 14, "endIndex": 15},
                                       "properties": {"pixelSize": 320}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": a, "dimension": "COLUMNS", "startIndex": 15, "endIndex": 16},
                                       "properties": {"pixelSize": 130}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": a, "dimension": "COLUMNS", "startIndex": 17, "endIndex": 18},
                                       "properties": {"pixelSize": 30}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": a, "dimension": "COLUMNS", "startIndex": 18, "endIndex": 19},
                                       "properties": {"pixelSize": 160}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": a, "dimension": "COLUMNS", "startIndex": 19, "endIndex": 31},
                                       "properties": {"pixelSize": 80}, "fields": "pixelSize"}},
        _chart(c, a, "Income vs spending by month", "COLUMN", "A12:A60",
               [("B12:B60", BLUE), ("C12:C60", ORANGE)], 0, 0, legend=True),
        _chart(c, a, "Net (income − spending) by month", "COLUMN", "A12:A60", [("D12:D60", BLUE)], 0, 9),
        _chart(c, a, "Spending by category", "BAR", "H12:H39", [("I12:I39", BLUE)], 22, 0, height=520),
    ]
    sh.batch_update({"requests": reqs})


def _chart(sheet_id, src_id, title, kind, domain, series, row, col, legend=False, height=380):
    return {"addChart": {"chart": {
        "spec": {
            "title": title,
            "titleTextFormat": {"bold": True},
            "basicChart": {
                "chartType": kind,
                "legendPosition": "BOTTOM_LEGEND" if legend else "NO_LEGEND",
                "headerCount": 1,
                "axis": [{"position": "BOTTOM_AXIS" if kind == "COLUMN" else "LEFT_AXIS"}],
                "domains": [{"domain": {"sourceRange": {"sources": [_grid(src_id, domain)]}}}],
                "series": [{"series": {"sourceRange": {"sources": [_grid(src_id, rng)]}},
                            "targetAxis": "LEFT_AXIS" if kind == "COLUMN" else "BOTTOM_AXIS",
                            "colorStyle": {"rgbColor": _rgb(color)}} for rng, color in series],
            },
        },
        "position": {"overlayPosition": {"anchorCell": {"sheetId": sheet_id, "rowIndex": row, "columnIndex": col},
                                         "widthPixels": 640, "heightPixels": height}},
    }}}
