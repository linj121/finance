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
- An unknown balance shows 'Missing data', never 0.

Layout: a scorecard of four habits (savings rate, emergency fund, card interest, net worth
change) over the last 12 complete months; the picked month vs the one before; spending by
group; balances; a 12-month trend table. Right side: spending by category and a drill-down.
Charts: one per habit, all drawn from the trend and category tables."""
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


# ---- Balances tab: month-end rows repeat a balance found on another row (see balances.py)
BAL_A, BAL_B, BAL_C, BAL_E = "Balances!$A$2:$A", "Balances!$B$2:$B", "Balances!$C$2:$C", "Balances!$E$2:$E"
NOT_MONTH_END = f'{BAL_E},"<>deposit month-end",{BAL_E},"<>card month-end"'


def month_end_sum(d: str, kinds=("deposit month-end", "card month-end")) -> str:
    return "+".join(f'SUMIFS({BAL_C},{BAL_B},{d},{BAL_E},"{k}")' for k in kinds)


LATEST_BALANCES = (f'LET(acc,UNIQUE(FILTER({BAL_A},{BAL_A}<>"")),'
                   f'd,MAP(acc,LAMBDA(a,MAXIFS({BAL_B},{BAL_A},a,{NOT_MONTH_END}))),'
                   f'b,MAP(acc,d,LAMBDA(a,x,SUMIFS({BAL_C},{BAL_A},a,{BAL_B},x,{NOT_MONTH_END}))),'
                   'HSTACK(acc,d,b))')

# ---- layout
LF = "$I$4"                       # first day of the last complete month
Y12 = f"({{MONTH}}>=EDATE({LF},-11))*({{MONTH}}<={LF})"   # the 12 complete months up to it
SC = 9                            # scorecard rows SC .. SC+8
MO = 21                           # month vs previous month
GR = 31                           # spending by group
BAL_ROW = 40                      # balances table (up to 10 accounts)
TREND = 56                        # first row of the 12-month table
TREND_END = TREND + 11
CAT_ROW, CAT_END = 9, 33          # category table (right side)
DRILL_ROW = 37
EF_TARGET = "IFERROR(N('Bills & Goals'!$B$9)/(N('Bills & Goals'!$B$9)>0),3)"   # months; 3 if not set

CELLS = {
    "A1": "Budget overview",
    "A2": "The scorecard checks four habits over the last 12 complete months. Pick a month in B4 for the "
          "month sections; budgets and savings goal are on the Plan tab. Explain any transaction in the "
          "Note column of Transactions.",

    "A4": "Month", "B4": "=$I$4",
    # every account's data reaches at least this date
    "C4": "Data complete to",
    "D4": f('=IFERROR(MIN(MAP(UNIQUE(FILTER(Balances!$A$2:$A,Balances!$A$2:$A<>"")),'
            'LAMBDA(a,MAXIFS(Balances!$B$2:$B,Balances!$A$2:$A,a)))),MAX({DATE}))'),
    "E4": "Uncategorized", "F4": f('=COUNTIF({CAT},"Uncategorized")'),
    "H4": "Last complete month", "I4": "=LET(d,$D$4,IF(d=EOMONTH(d,0),EOMONTH(d,-1)+1,EOMONTH(d,-2)+1))",
    "A5": '=IF(EOMONTH($B$4,0)<=$D$4,"Complete month","⚠ Partial month: data only to "'
          '&TEXT($D$4,"yyyy-mm-dd")&", so it is not comparable with a full month")',

    # ---- scorecard: one row per principle
    f"A{SC - 2}": f'="Scorecard: "&TEXT(EDATE({LF},-11),"mmm yyyy")&" to "&TEXT({LF},"mmm yyyy")',
    **{f"{c}{SC - 1}": h for c, h in zip("ABCDE", ["Measure", "Yours", "Target", "Status", "What it means"])},
    f"A{SC}": "Income", f"B{SC}": f"=SUMPRODUCT({Y12}*({{CAT}}=\"Income\"),{{AMT}})",
    f"A{SC + 1}": "Spending", f"B{SC + 1}": f"=-SUMPRODUCT({Y12}*{{SPEND}},{{AMT}})",
    f"E{SC + 1}": "Fixed + Essential + Optional. Transfers, card payments and money paid for others are left out",
    f"A{SC + 2}": "1. Savings rate", f"B{SC + 2}": f'=IF(B{SC}=0,"N/A",(B{SC}-B{SC + 1})/B{SC})', f"C{SC + 2}": 0.2,
    f"D{SC + 2}": f'=IF(ISNUMBER(B{SC + 2}),IF(B{SC + 2}>=C{SC + 2},"✓ On target","⚠ Below target"),"Missing data")',
    f"E{SC + 2}": "Share of income you kept. Spend less than you earn; 20% or more builds savings steadily",
    f"A{SC + 3}": "Essential spending per month",
    f"E{SC + 3}": "Fixed + Essential groups, 12-month average",
    f"A{SC + 4}": "Cash (chequing + savings)",
    f"B{SC + 4}": f'=IFERROR(SUM(MAP(UNIQUE(FILTER({BAL_A},{BAL_E}="deposit")),LAMBDA(a,SUMIFS({BAL_C},{BAL_A},a,'
                  f'{BAL_E},"deposit",{BAL_B},MAXIFS({BAL_B},{BAL_A},a,{BAL_E},"deposit"))))),"Missing data")',
    f"A{SC + 5}": "2. Emergency fund",
    f"B{SC + 5}": f'=IF(AND(ISNUMBER(B{SC + 4}),B{SC + 3}>0),B{SC + 4}/B{SC + 3},"Missing data")',
    f"C{SC + 5}": f"={EF_TARGET}",
    f"D{SC + 5}": f'=IF(ISNUMBER(B{SC + 5}),IF(B{SC + 5}>=C{SC + 5},"✓ On target","⚠ Below target"),"Missing data")',
    f"E{SC + 5}": "Months of essential spending your cash would cover. Aim for 3 to 6 (target: Bills & Goals B9)",
    f"A{SC + 6}": "3. Card interest paid",
    f"B{SC + 6}": f'=-SUMIFS({{AMT}},{{ACCT}},"*Credit*",{{DESC}},"*INTEREST*",{{MONTH}},">="&EDATE({LF},-11),'
                  f'{{MONTH}},"<="&{LF})',
    f"C{SC + 6}": 0,
    f"D{SC + 6}": f'=IF(B{SC + 6}<=0,"✓ None paid","⚠ Interest paid")',
    f"E{SC + 6}": "Interest means a card balance was carried. Pay the full statement every month",
    f"A{SC + 7}": "Net worth now", f"B{SC + 7}": f"=IFERROR(SUM(CHOOSECOLS({LATEST_BALANCES},3)),0)",
    f"E{SC + 7}": "Latest balance of every account (Balances tab); card debt is negative",
    f"A{SC + 8}": "4. Net worth change",
    f"B{SC + 8}": f'=LET(from_end,EOMONTH({LF},-12),to_end,EOMONTH({LF},0),IF(COUNTIFS({BAL_B},from_end,{BAL_E},"deposit month-end")=0,'
                  f'"Missing data",({month_end_sum("to_end")})-({month_end_sum("from_end")})))',
    f"C{SC + 8}": "Growing",
    f"D{SC + 8}": f'=IF(ISNUMBER(B{SC + 8}),IF(B{SC + 8}>0,"✓ Growing","⚠ Shrinking"),"Missing data")',
    f"E{SC + 8}": "Month-end net worth vs 12 months earlier. Invest what the first three leave over",

    # ---- the picked month vs the one before
    f"A{MO - 1}": '="Month: "&TEXT($B$4,"mmmm yyyy")&" vs the month before"',
    f"B{MO}": '=TEXT($B$4,"yyyy-mm")', f"C{MO}": '=TEXT(EDATE($B$4,-1),"yyyy-mm")', f"D{MO}": "Change",
    f"A{MO + 1}": "Income", f"A{MO + 2}": "Spending", f"A{MO + 3}": "Net (income − spending)",
    f"A{MO + 4}": "Savings rate", f"A{MO + 5}": "Savings goal (Plan)", f"A{MO + 6}": "Net vs goal",
    **{f"{c}{MO + 1}": f'=SUMIFS({{AMT}},{{CAT}},"Income",{{MONTH}},{m})' for c, m in (("B", "{M}"), ("C", "{LM}"))},
    **{f"{c}{MO + 2}": f"=-SUMPRODUCT(({{MONTH}}={m})*{{SPEND}},{{AMT}})" for c, m in (("B", "{M}"), ("C", "{LM}"))},
    **{f"{c}{MO + 3}": f"={c}{MO + 1}-{c}{MO + 2}" for c in "BC"},
    **{f"{c}{MO + 4}": f'=IF({c}{MO + 1}=0,"N/A",{c}{MO + 3}/{c}{MO + 1})' for c in "BC"},
    **{f"{c}{MO + 5}": f"={P_GOAL}" for c in "BC"},
    **{f"{c}{MO + 6}": f"={c}{MO + 3}-{c}{MO + 5}" for c in "BC"},
    **{f"D{MO + i}": f"=B{MO + i}-C{MO + i}" for i in (1, 2, 3, 6)},
    f"D{MO + 4}": f'=IF(OR(B{MO + 4}="N/A",C{MO + 4}="N/A"),"N/A",B{MO + 4}-C{MO + 4})',

    # ---- spending by parent group for the picked month
    f"A{GR - 1}": "Spending by group",
    **{f"{c}{GR}": h for c, h in zip("ABCDEF", ["Group", "This month", "Last month", "3-month avg", "Budget",
                                               "Remaining"])},
    **{f"A{GR + 1 + i}": g for i, g in enumerate(SPENDING_GROUPS)},
    **{k: v for i in range(len(SPENDING_GROUPS)) for k, v in {
        f"B{GR + 1 + i}": "=" + group_spend("{MONTH}={M}", f"$A{GR + 1 + i}"),
        f"C{GR + 1 + i}": "=" + group_spend("{MONTH}={LM}", f"$A{GR + 1 + i}"),
        f"D{GR + 1 + i}": "=" + group_spend("({MONTH}>=EDATE({M},-3))*({MONTH}<{M})", f"$A{GR + 1 + i}", 3),
        f"E{GR + 1 + i}": f"=IFERROR(SUMPRODUCT(ISNUMBER(MATCH({P_CAT},FILTER({{CATS}},{{GRP}}=$A{GR + 1 + i}),0))"
                          f"*{P_BUDGET}),0)",
        f"F{GR + 1 + i}": f'=IF(E{GR + 1 + i}=0,"",E{GR + 1 + i}-B{GR + 1 + i})',
    }.items()},
    f"A{GR + 5}": "Total", **{f"{c}{GR + 5}": f"=SUM({c}{GR + 1}:{c}{GR + 4})" for c in "BCDE"},
    f"F{GR + 5}": f'=IF(E{GR + 5}=0,"",E{GR + 5}-B{GR + 5})',

    # ---- latest balances, plus accounts you track by hand on Plan
    f"A{BAL_ROW - 2}": "Balances",
    f"A{BAL_ROW - 1}": "Account", f"B{BAL_ROW - 1}": "As of", f"C{BAL_ROW - 1}": "Balance",
    f"D{BAL_ROW - 1}": "Each balance is as of its own date; they don't line up exactly",
    f"A{BAL_ROW}": f'=ARRAYFORMULA(LET(stm,{LATEST_BALANCES},'
                   f'IFERROR(VSTACK(stm,FILTER(HSTACK({P_ACCT},IF({P_ASOF}="","—",{P_ASOF}),'
                   f'IF({P_BAL}="","Missing data",{P_BAL})),{P_ACCT}<>"")),stm)))',
    f"A{BAL_ROW + 11}": "Owed to me",
    f"B{BAL_ROW + 11}": f'=-SUMIFS({{AMT}},{{CAT}},"Paid for others",{{DATE}},">="&{P_SINCE})-{P_SETTLED}',
    f"C{BAL_ROW + 11}": f'="Paid for others since "&TEXT({P_SINCE},"yyyy-mm-dd")&", minus repaid outside '
                        f'(Plan). Not in net worth"',

    # ---- 12-month trend (feeds the charts)
    f"A{TREND - 2}": "Last 12 months",
    **{f"{c}{TREND - 1}": h for c, h in zip("ABCDEFGHIJKL", [
        "Month", "Income", "Spending", "Net", "Savings rate", "Fixed", "Essential", "Optional",
        "Cash (month end)", "Emergency target", "Net worth (month end)", "Status"])},
    f"A{TREND}": "=MAP(SEQUENCE(12,1,-11),LAMBDA(k,EDATE(EOMONTH($D$4,-1)+1,k)))",
}
_M = f"A{TREND}:A{TREND_END}"
_NODATA = "m<EOMONTH(MIN({DATE}),-1)+1"
CELLS.update({
    f"B{SC + 3}": "=(" + group_spend(Y12, '"Fixed"') + "+" + group_spend(Y12, '"Essential"') + ")/12",
    f"B{TREND}": f'=MAP({_M},LAMBDA(m,IF({_NODATA},"",SUMIFS({{AMT}},{{CAT}},"Income",{{MONTH}},m))))',
    f"C{TREND}": f'=MAP({_M},LAMBDA(m,IF({_NODATA},"",-SUMPRODUCT(({{MONTH}}=m)*{{SPEND}},{{AMT}}))))',
    f"D{TREND}": f'=MAP({_M},B{TREND}:B{TREND_END},C{TREND}:C{TREND_END},LAMBDA(m,i,s,IF(i="","",i-s)))',
    f"E{TREND}": f'=MAP(B{TREND}:B{TREND_END},D{TREND}:D{TREND_END},LAMBDA(i,n,IF(i="","",IF(i=0,"N/A",n/i))))',
    **{f"{c}{TREND}": f'=MAP({_M},LAMBDA(m,IF({_NODATA},"",{group_spend("{MONTH}=m", chr(34) + g + chr(34))})))'
       for c, g in zip("FGH", ["Fixed", "Essential", "Optional"])},
    # blank (a gap in the chart) when a month has no month-end balance yet
    f"I{TREND}": f'=MAP({_M},LAMBDA(m,LET(e,EOMONTH(m,0),IF(COUNTIFS({BAL_B},e,{BAL_E},"deposit month-end")=0,"",'
                 f'{month_end_sum("e", ("deposit month-end",))}))))',
    f"J{TREND}": f'=MAP(I{TREND}:I{TREND_END},LAMBDA(c,IF(c="","",IFERROR($C${SC + 5}*$B${SC + 3},""))))',
    f"K{TREND}": f'=MAP({_M},LAMBDA(m,LET(e,EOMONTH(m,0),IF(COUNTIFS({BAL_B},e,{BAL_E},"deposit month-end")=0,"",'
                 f'{month_end_sum("e")}))))',
    f"L{TREND}": f'=MAP({_M},LAMBDA(m,IF({_NODATA},"No data",IF(EOMONTH(m,0)>$D$4,'
                 f'"Partial (to "&TEXT($D$4,"mm-dd")&")",IF(m<MIN({{DATE}}),'
                 f'"Partial (from "&TEXT(MIN({{DATE}}),"mm-dd")&")","")))))',

    # ---- spending by category: last 12 months, and the picked month against its budget
    "N7": f'="Spending by category, "&TEXT(EDATE({LF},-11),"mmm yyyy")&" to "&TEXT({LF},"mmm yyyy")',
    **{f"{c}8": h for c, h in zip("NOPQRSTU", [
        "Category", "Group", "Last 12 months", "Per month", "Share", "Picked month", "Budget", "Remaining"])},
    f"N{CAT_ROW}": (
        '=LET(cats,FILTER({CATS},{GRP}<>"Not spending",{CATS}<>""),'
        'grp,MAP(cats,LAMBDA(c,XLOOKUP(c,{CATS},{GRP}))),'
        f'y,MAP(cats,LAMBDA(c,-SUMIFS({{AMT}},{{CAT}},c,{{MONTH}},">="&EDATE({LF},-11),{{MONTH}},"<="&{LF}))),'
        'pm,MAP(y,LAMBDA(v,v/12)),'
        'tot,SUM(y),'
        'shr,MAP(y,LAMBDA(v,IF(tot=0,0,v/tot))),'
        'sp,MAP(cats,LAMBDA(c,-SUMIFS({AMT},{CAT},c,{MONTH},{M}))),'
        f'bud,MAP(cats,LAMBDA(c,IF(COUNTIFS({P_CAT},c,{P_BUDGET},"<>")=0,"",SUMIFS({P_BUDGET},{P_CAT},c)))),'
        'rem,MAP(bud,sp,LAMBDA(b,s,IF(b="","",b-s))),'
        'SORT(HSTACK(cats,grp,y,pm,shr,sp,bud,rem),3,FALSE))'),

    # ---- drill-down: every transaction of one category in the picked month
    f"N{DRILL_ROW - 1}": "Transactions in category (pick one) →",
    f"O{DRILL_ROW - 1}": "Dining",
    f"P{DRILL_ROW - 1}": '="in "&TEXT($B$4,"yyyy-mm")',
    **{f"{c}{DRILL_ROW}": h for c, h in zip("NOPQR", ["Description", "Date", "Account", "Amount", "Note"])},
    f"N{DRILL_ROW + 1}": (f'=IFERROR(SORT(FILTER(HSTACK({{DESC}},{{DATE}},{{ACCT}},{{AMT}},{{NOTE}}),'
                          f'{{CAT}}=$O${DRILL_ROW - 1},{{MONTH}}={{M}}),2,TRUE),"No transactions")'),
})
CELLS = {k: f(v) if isinstance(v, str) else v for k, v in CELLS.items()}

MONEY = {"type": "CURRENCY", "pattern": "$#,##0;-$#,##0"}
PCT = {"type": "PERCENT", "pattern": "0%"}
MONTHS_FMT = {"type": "NUMBER", "pattern": '0.0 "months"'}
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
    muted = {"textFormat": {"italic": True, "foregroundColor": _rgb(INK_MUTED)}}
    sc_end = SC + 8
    reqs = [
        _fmt(a, "A1", {"textFormat": {"bold": True, "fontSize": 16}}, "textFormat"),
        _fmt(a, "A2", muted, "textFormat"),
        _fmt(a, f"E{SC}:E{sc_end}", muted, "textFormat"),
        _fmt(a, f"D{BAL_ROW - 1}", muted, "textFormat"),
        _fmt(a, f"C{BAL_ROW + 11}", muted, "textFormat"),
        *[_fmt(a, r, section, "textFormat") for r in (
            f"A{SC - 2}", f"A{MO - 1}", f"A{GR - 1}", f"A{BAL_ROW - 2}", f"A{TREND - 2}", "N7", f"N{DRILL_ROW - 1}")],
        *[_fmt(a, r, bold, "textFormat") for r in (
            "A4", "C4", "E4", "H4", "A5", f"A{SC - 1}:E{SC - 1}", f"A{SC + 2}:A{sc_end}",
            f"B{MO}:D{MO}", f"A{GR}:F{GR}", f"A{GR + 5}:F{GR + 5}", f"A{BAL_ROW - 1}:C{BAL_ROW - 1}",
            f"A{TREND - 1}:L{TREND - 1}", "N8:U8", f"N{DRILL_ROW}:R{DRILL_ROW}")],
        _fmt(a, "B4", {"numberFormat": MONTH_FMT, "backgroundColor": _rgb(INPUT_BG)}, "numberFormat,backgroundColor"),
        _fmt(a, f"O{DRILL_ROW - 1}", {"backgroundColor": _rgb(INPUT_BG)}, "backgroundColor"),
        _fmt(a, "D4", {"numberFormat": DAY_FMT}, "numberFormat"),
        _fmt(a, "I4", {"numberFormat": MONTH_FMT}, "numberFormat"),
        # scorecard
        _fmt(a, f"B{SC}:C{sc_end}", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, f"B{SC + 2}:C{SC + 2}", {"numberFormat": PCT}, "numberFormat"),
        _fmt(a, f"B{SC + 5}:C{SC + 5}", {"numberFormat": MONTHS_FMT}, "numberFormat"),
        _fmt(a, f"B{SC}:D{sc_end}", {"horizontalAlignment": "RIGHT"}, "horizontalAlignment"),
        # month vs month, groups, balances
        _fmt(a, f"B{MO + 1}:D{MO + 3}", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, f"B{MO + 4}:D{MO + 4}", {"numberFormat": PCT}, "numberFormat"),
        _fmt(a, f"B{MO + 5}:D{MO + 6}", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, f"B{GR + 1}:F{GR + 5}", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, f"B{BAL_ROW}:B{BAL_ROW + 9}", {"numberFormat": DAY_FMT}, "numberFormat"),
        _fmt(a, f"C{BAL_ROW}:C{BAL_ROW + 9}", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, f"B{BAL_ROW + 11}", {"numberFormat": MONEY}, "numberFormat"),
        # trend
        _fmt(a, f"A{TREND}:A{TREND_END}", {"numberFormat": MONTH_FMT, "horizontalAlignment": "LEFT"},
             "numberFormat,horizontalAlignment"),
        _fmt(a, f"B{TREND}:D{TREND_END}", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, f"E{TREND}:E{TREND_END}", {"numberFormat": PCT}, "numberFormat"),
        _fmt(a, f"F{TREND}:K{TREND_END}", {"numberFormat": MONEY}, "numberFormat"),
        # categories and drill-down
        _fmt(a, f"P{CAT_ROW}:Q{CAT_END}", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, f"R{CAT_ROW}:R{CAT_END}", {"numberFormat": PCT}, "numberFormat"),
        _fmt(a, f"S{CAT_ROW}:U{CAT_END}", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(a, f"O{DRILL_ROW + 1}:O250", {"numberFormat": DAY_FMT}, "numberFormat"),
        _fmt(a, f"Q{DRILL_ROW + 1}:Q250", {"numberFormat": MONEY}, "numberFormat"),
        # month picker: any month in the trend table; category picker: any category
        {"setDataValidation": {"range": _grid(a, "B4"), "rule": {
            "condition": {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": f"=$A${TREND}:$A${TREND_END}"}]},
            "showCustomUi": True, "strict": False}}},
        {"setDataValidation": {"range": _grid(a, f"O{DRILL_ROW - 1}"), "rule": {
            "condition": {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": "=Categories!$A$2:$A"}]},
            "showCustomUi": True, "strict": True}}},
        # status: the ✓ / ⚠ text carries the meaning; the fill only repeats it
        _when(a, f"D{SC}:D{sc_end}", f'=LEFT($D{SC},1)="✓"', GOOD_BG),
        _when(a, f"D{SC}:D{sc_end}", f'=OR(LEFT($D{SC},1)="⚠",$D{SC}="Missing data")', WARN_BG, 1),
        _when(a, f"U{CAT_ROW}:U{CAT_END}", f"=AND(ISNUMBER($U{CAT_ROW}),$U{CAT_ROW}<0)", OVER_BG),
        _when(a, f"F{GR + 1}:F{GR + 5}", f"=AND(ISNUMBER(F{GR + 1}),F{GR + 1}<0)", OVER_BG),
        _when(a, "A5", '=LEFT($A$5,1)="⚠"', WARN_BG),
        _when(a, f"L{TREND}:L{TREND_END}", f'=L{TREND}<>""', WARN_BG),
        _when(a, f"C{BAL_ROW}:C{BAL_ROW + 9}", f'=C{BAL_ROW}="Missing data"', WARN_BG),
        _width(a, 0, 0, 220), _width(a, 1, 3, 110), _width(a, 4, 4, 120), _width(a, 5, 11, 105),
        _width(a, 12, 12, 30), _width(a, 13, 13, 280), _width(a, 14, 20, 105),
        {"updateSheetProperties": {"properties": {"sheetId": a, "gridProperties": {"frozenRowCount": 5}},
                                   "fields": "gridProperties.frozenRowCount"}},
        # one chart per principle, all from the trend and category tables
        _chart(c, a, "1. Income vs spending per month", "COLUMN", f"A{TREND - 1}:A{TREND_END}",
               [(f"B{TREND - 1}:B{TREND_END}", BLUE), (f"C{TREND - 1}:C{TREND_END}", ORANGE)], 0, 0, legend=True),
        _chart(c, a, "2. Cash at month end vs emergency-fund target", "LINE", f"A{TREND - 1}:A{TREND_END}",
               [(f"I{TREND - 1}:I{TREND_END}", BLUE), (f"J{TREND - 1}:J{TREND_END}", INK_MUTED)], 0, 9, legend=True),
        _chart(c, a, "3. Spending by category, last 12 months", "BAR", f"N8:N{CAT_END}",
               [(f"P8:P{CAT_END}", BLUE)], 21, 0, height=520),
        _chart(c, a, "4. Net worth at month end", "LINE", f"A{TREND - 1}:A{TREND_END}",
               [(f"K{TREND - 1}:K{TREND_END}", BLUE)], 21, 9),
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
