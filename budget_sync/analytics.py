"""Builds the Analytics and Charts tabs, and the Plan tab if it doesn't exist yet.
Analytics is all live formulas over the Transactions, Categories and Balances tabs, so
it refreshes after every sync (and when you change a category in the sheet); rebuilding
it only matters when this layout changes. Plan holds your own inputs (budgets, savings
goal, accounts with no statements) and is never overwritten.

Rules the formulas follow:
- Spending and income leave out categories in the 'Not spending' group (transfers between
  your accounts, card payments, money paid for others); refunds lower their own category.
- Net = income - spending; savings rate = net / income, 'N/A' when income is 0.
- A month that the data doesn't fully cover is labelled partial, never silently compared.
- An unknown balance shows 'Missing data', never 0."""
import gspread

ANALYTICS = "Analytics"
CHARTS = "Charts"
PLAN = "Plan"
SPENDING_GROUPS = ["Fixed", "Essential", "Optional", "Uncategorized"]

# starting monthly budgets for the Plan tab (blank = no budget); edit them there
BUDGETS = {"Housing": 1250, "Phone & Internet": 100, "Insurance": 25, "Subscriptions": 60,
           "Groceries": 400, "Utilities": 130, "Transportation": 250, "Health": 65, "Education": 100,
           "Fees & Interest": 0, "Dining": 350, "Shopping": 250, "Entertainment": 150, "Travel": 100}
SAVINGS_GOAL = 1000
OWED_SINCE = "2026-05-01"          # first King William rent paid for friends
MANUAL_ACCOUNTS = ["Simplii"]      # accounts with no statements loaded

# validated categorical slots 1-3 and blue sequential ramp (dataviz reference palette)
BLUE, ORANGE, TEAL = "#2a78d6", "#eb6834", "#1f9e89"
RAMP_LOW, RAMP_HIGH = "#fcfcfb", "#6da7ec"
INK_MUTED = "#52514e"
INPUT_BG = "#fff8e1"
GOOD_BG, WARN_BG, OVER_BG, NOTE_BG = "#e6f4ea", "#fef3d6", "#fbe3e1", "#fdebd8"

# Plan tab cells the formulas read
P_GOAL, P_SINCE, P_SETTLED = "Plan!$B$4", "Plan!$B$5", "Plan!$B$6"
P_CAT, P_BUDGET = "Plan!$A$10:$A$60", "Plan!$C$10:$C$60"
P_ACCT, P_BAL, P_ASOF = "Plan!$E$10:$E$20", "Plan!$F$10:$F$20", "Plan!$G$10:$G$20"

# shorthands expanded in the formulas below
SUBS = {
    "DATE": "Transactions!$B$2:$B", "MONTH": "Transactions!$C$2:$C", "ACCT": "Transactions!$D$2:$D",
    "DESC": "Transactions!$E$2:$E", "AMT": "Transactions!$F$2:$F", "CAT": "Transactions!$H$2:$H",
    "NOTE": "Transactions!$K$2:$K", "CATS": "Categories!$A$2:$A", "GRP": "Categories!$B$2:$B",
    "M": "$B$4", "LM": "EDATE($B$4,-1)",
}
# 1 for rows that count as spending: categorized, and not in the 'Not spending' group
SUBS["SPEND"] = '(({CAT}<>"")*ISNA(MATCH({CAT},FILTER({CATS},{GRP}="Not spending"),0)))'


def f(formula: str) -> str:
    for _ in range(2):   # SPEND contains shorthands itself
        for k, v in SUBS.items():
            formula = formula.replace("{" + k + "}", v)
    return formula


def in_group(group_ref: str) -> str:
    """1 for transactions whose category belongs to the group named in group_ref."""
    return f"ISNUMBER(MATCH({{CAT}},FILTER({{CATS}},{{GRP}}={group_ref}),0))"


def group_spend(month_cond: str, group_ref: str, divide: int = 1) -> str:
    s = f"-SUMPRODUCT(({month_cond})*{in_group(group_ref)},{{AMT}})"
    return f"IFERROR({s}{'/' + str(divide) if divide > 1 else ''},0)"


TREND = 49           # first row of the 12-month table
TREND_END = TREND + 11
CAT_ROW, CAT_END = 9, 33
BIG_ROW = 36
DRILL_ROW = 53
BAL_ROW = 33

CELLS = {
    "A1": "Budget overview",
    "A2": "Pick a month in B4 (first day of the month). Budgets, savings goal and accounts without "
          "statements are on the Plan tab. Explain any transaction in the Note column of Transactions.",

    "A4": "Month", "B4": "=LET(d,$D$4,IF(d=EOMONTH(d,0),EOMONTH(d,-1)+1,EOMONTH(d,-2)+1))",
    # every account's statements reach at least this date
    "C4": "Data complete to",
    "D4": f('=IFERROR(MIN(MAP(UNIQUE(FILTER(Balances!$A$2:$A,Balances!$A$2:$A<>"")),'
            'LAMBDA(a,MAXIFS(Balances!$B$2:$B,Balances!$A$2:$A,a)))),MAX({DATE}))'),
    "E4": "Uncategorized", "F4": f('=COUNTIF({CAT},"Uncategorized")'),
    "A5": '=IF(EOMONTH($B$4,0)<=$D$4,"Complete month","⚠ Partial month: data only to "'
          '&TEXT($D$4,"yyyy-mm-dd")&", so it is not comparable with a full month")',

    # ---- this month vs last month
    "A7": "This month vs last month",
    "B8": '=TEXT($B$4,"yyyy-mm")', "C8": '=TEXT(EDATE($B$4,-1),"yyyy-mm")', "D8": "Change",
    "A9": "Income", "A10": "Spending", "A11": "Net (income − spending)", "A12": "Savings rate",
    "A13": "Savings goal", "A14": "Net vs goal",
    **{f"{c}9": f'=SUMIFS({{AMT}},{{CAT}},"Income",{{MONTH}},{m})' for c, m in (("B", "{M}"), ("C", "{LM}"))},
    **{f"{c}10": f"=-SUMPRODUCT(({{MONTH}}={m})*{{SPEND}},{{AMT}})" for c, m in (("B", "{M}"), ("C", "{LM}"))},
    **{f"{c}11": f"={c}9-{c}10" for c in "BC"},
    **{f'{c}12': f'=IF({c}9=0,"N/A",{c}11/{c}9)' for c in "BC"},
    **{f"{c}13": f"={P_GOAL}" for c in "BC"},
    **{f"{c}14": f"={c}11-{c}13" for c in "BC"},
    "D9": "=B9-C9", "D10": "=B10-C10", "D11": "=B11-C11",
    "D12": '=IF(OR(B12="N/A",C12="N/A"),"N/A",B12-C12)', "D14": "=B14-C14",

    # ---- spending by parent group
    "A16": "Spending by group",
    "A17": "Group", "B17": "This month", "C17": "Last month", "D17": "3-month avg", "E17": "Budget",
    "F17": "Remaining",
    **{f"A{18 + i}": g for i, g in enumerate(SPENDING_GROUPS)},
    **{k: v for i in range(len(SPENDING_GROUPS)) for k, v in {
        f"B{18 + i}": "=" + group_spend("{MONTH}={M}", f"$A{18 + i}"),
        f"C{18 + i}": "=" + group_spend("{MONTH}={LM}", f"$A{18 + i}"),
        f"D{18 + i}": "=" + group_spend("({MONTH}>=EDATE({M},-3))*({MONTH}<{M})", f"$A{18 + i}", 3),
        f"E{18 + i}": f"=IFERROR(SUMPRODUCT(ISNUMBER(MATCH({P_CAT},FILTER({{CATS}},{{GRP}}=$A{18 + i}),0))"
                      f"*{P_BUDGET}),0)",
        f"F{18 + i}": f'=IF(E{18 + i}=0,"",E{18 + i}-B{18 + i})',
    }.items()},
    "A22": "Total", **{f"{c}22": f"=SUM({c}18:{c}21)" for c in "BCDE"}, "F22": '=IF(E22=0,"",E22-B22)',

    # ---- balances and net worth
    "A24": "Balances & net worth",
    "A25": "Assets", "B25": f"=SUMIF(C{BAL_ROW}:C{BAL_ROW + 12},\">0\")",
    "A26": "Debts", "B26": f"=SUMIF(C{BAL_ROW}:C{BAL_ROW + 12},\"<0\")",
    "A27": "Owed to me", "B27": f'=-SUMIFS({{AMT}},{{CAT}},"Paid for others",{{DATE}},">="&{P_SINCE})-{P_SETTLED}',
    "C27": f'="Paid for others since "&TEXT({P_SINCE},"yyyy-mm-dd")&", minus repaid outside (Plan)"',
    "A28": "Net worth", "B28": "=B25+B26+B27",
    "A29": f'=IFERROR("⚠ Missing data, not included: "&TEXTJOIN(", ",TRUE,FILTER({P_ACCT},{P_ACCT}<>"",'
           f'{P_BAL}="")),"")',
    "A30": "Each balance is as of its own statement date; the dates don't line up exactly.",
    f"A{BAL_ROW - 1}": "Account", f"B{BAL_ROW - 1}": "As of", f"C{BAL_ROW - 1}": "Balance",
    f"A{BAL_ROW}": f'=ARRAYFORMULA(LET(acc,UNIQUE(FILTER(Balances!$A$2:$A,Balances!$A$2:$A<>"")),'
                   'd,MAP(acc,LAMBDA(a,MAXIFS(Balances!$B$2:$B,Balances!$A$2:$A,a))),'
                   'b,MAP(acc,d,LAMBDA(a,x,SUMIFS(Balances!$C$2:$C,Balances!$A$2:$A,a,Balances!$B$2:$B,x))),'
                   'stm,HSTACK(acc,d,b),'
                   f'IFERROR(VSTACK(stm,FILTER(HSTACK({P_ACCT},IF({P_ASOF}="","—",{P_ASOF}),'
                   f'IF({P_BAL}="","Missing data",{P_BAL})),{P_ACCT}<>"")),stm)))',

    # ---- 12-month trend
    f"A{TREND - 2}": "Last 12 months",
    **{f"{c}{TREND - 1}": h for c, h in zip("ABCDEFGHI", [
        "Month", "Income", "Spending", "Net", "Savings rate", "Fixed", "Essential", "Optional", "Status"])},
    f"A{TREND}": "=MAP(SEQUENCE(12,1,-11),LAMBDA(k,EDATE(EOMONTH($D$4,-1)+1,k)))",
}
_M = f"A{TREND}:A{TREND_END}"
_NODATA = f"m<EOMONTH(MIN({{DATE}}),-1)+1"
CELLS.update({
    f"B{TREND}": f(f'=MAP({_M},LAMBDA(m,IF({_NODATA},"",SUMIFS({{AMT}},{{CAT}},"Income",{{MONTH}},m))))'),
    f"C{TREND}": f(f'=MAP({_M},LAMBDA(m,IF({_NODATA},"",-SUMPRODUCT(({{MONTH}}=m)*{{SPEND}},{{AMT}}))))'),
    f"D{TREND}": f'=MAP({_M},B{TREND}:B{TREND_END},C{TREND}:C{TREND_END},LAMBDA(m,i,s,IF(i="","",i-s)))',
    f"E{TREND}": f'=MAP(B{TREND}:B{TREND_END},D{TREND}:D{TREND_END},LAMBDA(i,n,IF(i="","",IF(i=0,"N/A",n/i))))',
    **{f"{c}{TREND}": f(f'=MAP({_M},LAMBDA(m,IF({_NODATA},"",{group_spend("{MONTH}=m", chr(34) + g + chr(34))})))')
       for c, g in zip("FGH", ["Fixed", "Essential", "Optional"])},
    f"I{TREND}": f(f'=MAP({_M},LAMBDA(m,IF({_NODATA},"No data",IF(EOMONTH(m,0)>$D$4,'
                   f'"Partial (to "&TEXT($D$4,"mm-dd")&")",IF(m<MIN({{DATE}}),'
                   f'"Partial (from "&TEXT(MIN({{DATE}}),"mm-dd")&")","")))))'),

    # ---- budget by category for the month
    "K7": '="Budget by category, "&TEXT($B$4,"yyyy-mm")',
    **{f"{c}8": h for c, h in zip("KLMNOPQRSTU", [
        "Category", "Group", "Budget", "Spent", "Remaining", "% used", "Share", "# txns",
        "Last month", "3-month avg", "vs 3-month avg"])},
    f"K{CAT_ROW}": f(
        '=LET(cats,FILTER({CATS},{GRP}<>"Not spending",{CATS}<>""),'
        'grp,MAP(cats,LAMBDA(c,XLOOKUP(c,{CATS},{GRP}))),'
        f'bud,MAP(cats,LAMBDA(c,IF(COUNTIFS({P_CAT},c,{P_BUDGET},"<>")=0,"",SUMIFS({P_BUDGET},{P_CAT},c)))),'
        'sp,MAP(cats,LAMBDA(c,-SUMIFS({AMT},{CAT},c,{MONTH},{M}))),'
        'tot,SUM(sp),'
        'rem,MAP(bud,sp,LAMBDA(b,s,IF(b="","",b-s))),'
        'pct,MAP(bud,sp,LAMBDA(b,s,IF(b="","",IF(b=0,"",s/b)))),'
        'shr,MAP(sp,LAMBDA(s,IF(tot=0,0,s/tot))),'
        'n,MAP(cats,LAMBDA(c,COUNTIFS({CAT},c,{MONTH},{M}))),'
        'lm,MAP(cats,LAMBDA(c,-SUMIFS({AMT},{CAT},c,{MONTH},{LM}))),'
        'av,MAP(cats,LAMBDA(c,-SUMIFS({AMT},{CAT},c,{MONTH},">="&EDATE({M},-3),{MONTH},"<"&{M})/3)),'
        'dv,MAP(sp,av,LAMBDA(s,a,s-a)),'
        'SORT(HSTACK(cats,grp,bud,sp,rem,pct,shr,n,lm,av,dv),4,FALSE))'),

    # ---- largest expenses of the month
    f"K{BIG_ROW - 2}": '="Largest expenses, "&TEXT($B$4,"yyyy-mm")',
    **{f"{c}{BIG_ROW - 1}": h for c, h in zip("KLMNO", ["Description", "Date", "Category", "Amount", "Note"])},
    f"K{BIG_ROW}": f('=ARRAYFORMULA(IFERROR(SORTN(FILTER(HSTACK({DESC},{DATE},{CAT},-{AMT},{NOTE}),'
                     '{MONTH}={M},{AMT}<0,{SPEND}),15,0,4,FALSE),"No spending this month"))'),

    # ---- drill-down: every transaction of one category in the month
    f"K{DRILL_ROW - 1}": "Transactions in category (pick one) →",
    f"L{DRILL_ROW - 1}": "Dining",
    f"M{DRILL_ROW - 1}": '="in "&TEXT($B$4,"yyyy-mm")',
    **{f"{c}{DRILL_ROW}": h for c, h in zip("KLMNO", ["Description", "Date", "Account", "Amount", "Note"])},
    f"K{DRILL_ROW + 1}": f(f'=IFERROR(SORT(FILTER(HSTACK({{DESC}},{{DATE}},{{ACCT}},{{AMT}},{{NOTE}}),'
                           f'{{CAT}}=$L${DRILL_ROW - 1},{{MONTH}}={{M}}),2,TRUE),"No transactions")'),
})
CELLS = {k: f(v) if isinstance(v, str) else v for k, v in CELLS.items()}

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


def _width(sid, first: int, last: int, px: int) -> dict:
    return {"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": first, "endIndex": last + 1},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}}


def _when(sid, a1, formula, color, index=0):
    return {"addConditionalFormatRule": {"index": index, "rule": {
        "ranges": [_grid(sid, a1)],
        "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                        "format": {"backgroundColor": _rgb(color)}}}}}


def _recreate(sh: gspread.Spreadsheet, title: str, index: int) -> gspread.Worksheet:
    try:
        sh.del_worksheet(sh.worksheet(title))
    except gspread.WorksheetNotFound:
        pass
    return sh.add_worksheet(title, rows=250, cols=32, index=index)


def build(sh: gspread.Spreadsheet, groups: dict[str, str]) -> bool:
    """groups: category -> parent group, in rules.yaml order (used to start the Plan tab).
    Returns True if the Plan tab was created."""
    plan_created = _ensure_plan(sh, groups)
    ws = _recreate(sh, ANALYTICS, 0)
    charts = _recreate(sh, CHARTS, 1)
    ws.batch_update([{"range": k, "values": [[v]]} for k, v in CELLS.items()], value_input_option="USER_ENTERED")

    a, c = ws.id, charts.id
    bold = {"textFormat": {"bold": True}}
    section = {"textFormat": {"bold": True, "fontSize": 12}}
    reqs = [
        _fmt(a, "A1", {"textFormat": {"bold": True, "fontSize": 16}}, "textFormat"),
        _fmt(a, "A2", {"textFormat": {"italic": True, "foregroundColor": _rgb(INK_MUTED)}}, "textFormat"),
        _fmt(a, "A30", {"textFormat": {"italic": True, "foregroundColor": _rgb(INK_MUTED)}}, "textFormat"),
        *[_fmt(a, r, section, "textFormat") for r in (
            "A7", "A16", "A24", f"A{TREND - 2}", "K7", f"K{BIG_ROW - 2}", f"K{DRILL_ROW - 1}")],
        *[_fmt(a, r, bold, "textFormat") for r in (
            "A4", "C4", "E4", "A5", "B8:D8", "A17:F17", "A22:F22", "A28:B28", f"A{BAL_ROW - 1}:C{BAL_ROW - 1}",
            f"A{TREND - 1}:I{TREND - 1}", "K8:U8", f"K{BIG_ROW - 1}:O{BIG_ROW - 1}", f"K{DRILL_ROW}:O{DRILL_ROW}")],
        _fmt(a, "B4", {"numberFormat": MONTH_FMT, "backgroundColor": _rgb(INPUT_BG)}, "numberFormat,backgroundColor"),
        _fmt(a, f"L{DRILL_ROW - 1}", {"backgroundColor": _rgb(INPUT_BG)}, "backgroundColor"),
        _fmt(a, "D4", {"numberFormat": DAY_FMT}, "numberFormat"),
        _fmt(a, "B9:D11", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, "B12:D12", {"numberFormat": PCT}, "numberFormat"),
        _fmt(a, "B13:D14", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, "B18:F22", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, "B25:B28", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, f"B{BAL_ROW}:B{BAL_ROW + 12}", {"numberFormat": DAY_FMT}, "numberFormat"),
        _fmt(a, f"C{BAL_ROW}:C{BAL_ROW + 12}", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, f"A{TREND}:A{TREND_END}", {"numberFormat": MONTH_FMT, "horizontalAlignment": "LEFT"},
             "numberFormat,horizontalAlignment"),
        _fmt(a, f"B{TREND}:D{TREND_END}", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, f"E{TREND}:E{TREND_END}", {"numberFormat": PCT}, "numberFormat"),
        _fmt(a, f"F{TREND}:H{TREND_END}", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, f"M{CAT_ROW}:O{CAT_END}", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, f"P{CAT_ROW}:Q{CAT_END}", {"numberFormat": PCT}, "numberFormat"),
        _fmt(a, f"S{CAT_ROW}:U{CAT_END}", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, f"L{BIG_ROW}:L{BIG_ROW + 14}", {"numberFormat": DAY_FMT}, "numberFormat"),
        _fmt(a, f"N{BIG_ROW}:N{BIG_ROW + 14}", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, f"L{DRILL_ROW + 1}:L250", {"numberFormat": DAY_FMT}, "numberFormat"),
        _fmt(a, f"N{DRILL_ROW + 1}:N250", {"numberFormat": MONEY}, "numberFormat"),
        # month picker: any month in the trend table; category picker: any category
        {"setDataValidation": {"range": _grid(a, "B4"), "rule": {
            "condition": {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": f"=$A${TREND}:$A${TREND_END}"}]},
            "showCustomUi": True, "strict": False}}},
        {"setDataValidation": {"range": _grid(a, f"L{DRILL_ROW - 1}"), "rule": {
            "condition": {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": "=Categories!$A$2:$A"}]},
            "showCustomUi": True, "strict": True}}},
        # budget use: over / close / fine (only where a budget is set)
        _when(a, f"P{CAT_ROW}:P{CAT_END}", f'=AND(ISNUMBER($P{CAT_ROW}),$P{CAT_ROW}>1)', OVER_BG),
        _when(a, f"P{CAT_ROW}:P{CAT_END}", f'=AND(ISNUMBER($P{CAT_ROW}),$P{CAT_ROW}>=0.8)', WARN_BG, 1),
        _when(a, f"P{CAT_ROW}:P{CAT_END}", f'=ISNUMBER($P{CAT_ROW})', GOOD_BG, 2),
        _when(a, "F18:F22", "=AND(ISNUMBER(F18),F18<0)", OVER_BG),
        # well above its own recent level: a fact to look at, not a verdict
        _when(a, f"U{CAT_ROW}:U{CAT_END}",
              f"=AND(ISNUMBER($U{CAT_ROW}),$U{CAT_ROW}>100,$U{CAT_ROW}>0.5*$T{CAT_ROW})", NOTE_BG),
        _when(a, "A5", '=LEFT($A$5,1)="⚠"', WARN_BG),
        _when(a, f"I{TREND}:I{TREND_END}", f'=I{TREND}<>""', WARN_BG),
        _when(a, f"C{BAL_ROW}:C{BAL_ROW + 12}", f'=C{BAL_ROW}="Missing data"', WARN_BG),
        _when(a, "A29", '=A29<>""', WARN_BG),
        _width(a, 0, 0, 190), _width(a, 1, 8, 105), _width(a, 9, 9, 30),
        _width(a, 10, 10, 280), _width(a, 11, 20, 95),
        {"updateSheetProperties": {"properties": {"sheetId": a, "gridProperties": {"frozenRowCount": 5}},
                                   "fields": "gridProperties.frozenRowCount"}},
        _chart(c, a, "Income vs spending, last 12 months", "COLUMN", f"A{TREND - 1}:A{TREND_END}",
               [(f"B{TREND - 1}:B{TREND_END}", BLUE), (f"C{TREND - 1}:C{TREND_END}", ORANGE)], 0, 0, legend=True),
        _chart(c, a, "Net (income − spending) by month", "COLUMN", f"A{TREND - 1}:A{TREND_END}",
               [(f"D{TREND - 1}:D{TREND_END}", BLUE)], 0, 9),
        _chart(c, a, "Spending by group per month", "COLUMN", f"A{TREND - 1}:A{TREND_END}",
               [(f"F{TREND - 1}:F{TREND_END}", BLUE), (f"G{TREND - 1}:G{TREND_END}", TEAL),
                (f"H{TREND - 1}:H{TREND_END}", ORANGE)], 21, 0, legend=True, stacked=True),
        _chart(c, a, "Spending by category, selected month", "BAR", f"K8:K{CAT_END}",
               [(f"N8:N{CAT_END}", BLUE)], 21, 9, height=520),
    ]
    sh.batch_update({"requests": reqs})
    return plan_created


def _ensure_plan(sh: gspread.Spreadsheet, groups: dict[str, str]) -> bool:
    """Create the Plan tab with starting values, once. Returns True if it was created."""
    try:
        sh.worksheet(PLAN)
        return False
    except gspread.WorksheetNotFound:
        pass
    ws = sh.add_worksheet(PLAN, rows=80, cols=8, index=2)
    cats = [c for g in SPENDING_GROUPS[:3] for c, cg in groups.items() if cg == g]
    rows = {
        "A1": [["Plan"]],
        "A2": [["Your inputs. Edit the yellow cells; the program never overwrites this tab."]],
        "A4": [["Savings goal per month", SAVINGS_GOAL]],
        "A5": [["Track 'owed to me' from", OWED_SINCE]],
        "A6": [["Repaid to you outside these accounts (cash, WeChat…)", 0]],
        "A8": [["Monthly budget per category (blank = no budget)"]],
        "A9": [["Category", "Group", "Monthly budget"]],
        "A10": [[c, f'=IFERROR(VLOOKUP(A{10 + i},Categories!$A:$B,2,FALSE),"")', BUDGETS.get(c, "")]
                for i, c in enumerate(cats)],
        "E8": [["Accounts without statements (fill in to count them in net worth)"]],
        "E9": [["Account", "Balance", "As of"]],
        "E10": [[a, "", ""] for a in MANUAL_ACCOUNTS],
    }
    ws.batch_update([{"range": k, "values": v} for k, v in rows.items()], value_input_option="USER_ENTERED")
    p = ws.id
    sh.batch_update({"requests": [
        _fmt(p, "A1", {"textFormat": {"bold": True, "fontSize": 16}}, "textFormat"),
        _fmt(p, "A2", {"textFormat": {"italic": True, "foregroundColor": _rgb(INK_MUTED)}}, "textFormat"),
        *[_fmt(p, r, {"textFormat": {"bold": True}}, "textFormat") for r in ("A8", "A9:C9", "E8", "E9:G9")],
        _fmt(p, "B4:B6", {"backgroundColor": _rgb(INPUT_BG)}, "backgroundColor"),
        _fmt(p, "B4", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(p, "B5", {"numberFormat": DAY_FMT}, "numberFormat"),
        _fmt(p, "B6", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(p, "C10:C60", {"numberFormat": MONEY, "backgroundColor": _rgb(INPUT_BG)}, "numberFormat,backgroundColor"),
        _fmt(p, "E10:G20", {"backgroundColor": _rgb(INPUT_BG)}, "backgroundColor"),
        _fmt(p, "F10:F20", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(p, "G10:G20", {"numberFormat": DAY_FMT}, "numberFormat"),
        _width(p, 0, 0, 330), _width(p, 1, 2, 120), _width(p, 3, 3, 30), _width(p, 4, 4, 160), _width(p, 5, 6, 110),
    ]})
    return True


def _chart(sheet_id, src_id, title, kind, domain, series, row, col, legend=False, height=380, stacked=False):
    upright = kind != "BAR"   # COLUMN / LINE: categories along the bottom
    spec = {
        "chartType": kind,
        "legendPosition": "BOTTOM_LEGEND" if legend else "NO_LEGEND",
        "headerCount": 1,
        "axis": [{"position": "BOTTOM_AXIS" if upright else "LEFT_AXIS"}],
        "domains": [{"domain": {"sourceRange": {"sources": [_grid(src_id, domain)]}}}],
        "series": [{"series": {"sourceRange": {"sources": [_grid(src_id, rng)]}},
                    "targetAxis": "LEFT_AXIS" if upright else "BOTTOM_AXIS",
                    "colorStyle": {"rgbColor": _rgb(color)}} for rng, color in series],
    }
    if stacked:
        spec["stackedType"] = "STACKED"
    return {"addChart": {"chart": {
        "spec": {"title": title, "titleTextFormat": {"bold": True}, "basicChart": spec},
        "position": {"overlayPosition": {"anchorCell": {"sheetId": sheet_id, "rowIndex": row, "columnIndex": col},
                                         "widthPixels": 640, "heightPixels": height}},
    }}}
