"""Bills & Goals (your inputs) and Outlook (live formulas): next-30-day cash flow,
recurring commitments, emergency fund, money set aside for one-off costs and goals,
and the credit card.

Rules the formulas follow:
- Only items you ticked 'Confirmed' on Bills & Goals count. Suggestions from history start
  unconfirmed: a pattern in past statements is not a fact about the future.
- Money already set aside (emergency fund, one-off costs, goals) is subtracted before
  anything is called unallocated, so it is never counted twice.
- A missing amount, date or balance shows 'Missing data', never 0."""
import calendar
from datetime import date

import gspread

from .analytics import (INK_MUTED, INPUT_BG, MONEY, DAY_FMT, PCT, BLUE, OVER_BG, WARN_BG, GOOD_BG,
                        _chart, _fmt, _grid, _recreate, _rgb, _when, _width, f)
from .recurring import Suggestion

INPUTS = "Bills & Goals"
OUTLOOK = "Outlook"
DAYS = 30
EVERYDAY_CATEGORIES = ["Groceries", "Dining", "Transportation", "Shopping", "Entertainment", "Health"]
FREQUENCIES = ["Monthly", "Biweekly", "Weekly", "Yearly", "Once"]

BG = "'Bills & Goals'!"
# settings
START, CASH_IN, EVERYDAY, CARD_MODE, EF_ACCT, EF_MONTHS = (BG + f"$B${r}" for r in range(4, 10))
# recurring income & bills, rows 13-42
R0, R1 = 13, 42
NAME, TYPE, AMT, FREQ, DT, FROM, CONF = (f"{BG}${c}${R0}:${c}${R1}" for c in "ABCDEFG")
# one-off costs (rows 13-27) and savings goals (rows 32-39): name, amount, date, set aside
S0, S1, G0, G1 = 13, 27, 32, 39
SINK = [f"{BG}${c}${S0}:${c}${S1}" for c in "KLMN"]
GOAL = [f"{BG}${c}${G0}:${c}${G1}" for c in "KLMN"]

# latest balance of each account
LATEST = ("MAP(UNIQUE(FILTER(Balances!$A$2:$A,Balances!$E$2:$E=\"{kind}\")),LAMBDA(a,SUMIFS(Balances!$C$2:$C,"
          "Balances!$A$2:$A,a,Balances!$B$2:$B,MAXIFS(Balances!$B$2:$B,Balances!$A$2:$A,a))))")
DEPOSITS = "IFERROR(SUM(" + LATEST.format(kind="deposit") + "),0)"
CASH0 = f'IF({CASH_IN}="",{DEPOSITS},{CASH_IN})'


def card(col: int) -> str:
    """Column `col` (1 = A ... 9 = I) of the latest card statement's row in Balances."""
    return f'INDEX(SORT(FILTER(Balances!$A$2:$I,Balances!$E$2:$E="card"),2,FALSE),1,{col})'
# last complete month (same rule as Analytics)
LASTFULL = "LET(d,Analytics!$D$4,IF(d=EOMONTH(d,0),EOMONTH(d,-1)+1,EOMONTH(d,-2)+1))"


def occurs(d: str) -> str:
    """1 for each confirmed Bills & Goals row that falls on day d."""
    return (f'((({FREQ}="Monthly")*(DAY({d})=IF(DAY({DT})>DAY(EOMONTH({d},0)),DAY(EOMONTH({d},0)),DAY({DT}))))'
            f'+(({FREQ}="Biweekly")*(MOD({d}-{DT},14)=0)*({d}>={DT}))'
            f'+(({FREQ}="Weekly")*(MOD({d}-{DT},7)=0)*({d}>={DT}))'
            f'+(({FREQ}="Yearly")*(MONTH({d})=MONTH({DT}))*(DAY({d})=DAY({DT})))'
            f'+(({FREQ}="Once")*({d}={DT})))*({CONF}=TRUE)*({DT}<>"")')


PER_MONTH = (f'(({FREQ}="Monthly")+({FREQ}="Biweekly")*26/12+({FREQ}="Weekly")*52/12'
             f'+({FREQ}="Yearly")/12)')


def months_avg(months: int, cond: str) -> str:
    """Average per month over the last `months` complete months of -amount where cond."""
    return (f"LET(l,{LASTFULL},-SUMPRODUCT(({{MONTH}}>=EDATE(l,-{months - 1}))*({{MONTH}}<=l)*{cond},{{AMT}})/{months})")


INCOME3 = (f'LET(l,{LASTFULL},SUMIFS({{AMT}},{{CAT}},"Income",{{MONTH}},">="&EDATE(l,-2),{{MONTH}},"<="&l)/3)')
SPEND3 = months_avg(3, "{SPEND}")
ESSENTIAL6 = months_avg(6, 'ISNUMBER(MATCH({CAT},FILTER({CATS},({GRP}="Fixed")+({GRP}="Essential")),0))')

T0, T1 = 17, 16 + DAYS          # daily table rows
FUND_ROW = T1 + 4               # one-off costs table
GOAL_ROW = FUND_ROW + 19        # savings goals table
AFFORD_ROW = GOAL_ROW + 13      # can I afford my plans?


def _plan_table(cols: list[str]) -> str:
    """Name | Amount | Due | Set aside | Still needed | Months left | Per month | Status."""
    n, amt, due, got = cols
    return (f'=ARRAYFORMULA(IFERROR(LET(n,{n},a,{amt},d,{due},g,IF({got}="",0,{got}),'
            'need,IF(a="","",IF(a-g<0,0,a-g)),'
            'left,IF(d="","",IF(d<=TODAY(),0,IF(DATEDIF(TODAY(),d,"M")<1,1,DATEDIF(TODAY(),d,"M")))),'
            'pm,IF((need="")+(left=""),"",IF(left=0,need,need/left)),'
            'st,IF((a="")+(d=""),"Missing data",IF(need=0,"Fully set aside",IF(d<=TODAY(),"Due now","On the way"))),'
            'FILTER(HSTACK(n,a,d,g,need,left,pm,st),n<>"")),"Nothing added yet"))')


CELLS = {
    "A1": "Outlook",
    "A2": f'=COUNTIFS({NAME},"<>",{CONF},FALSE)&" suggested item(s) on Bills & Goals are not confirmed yet, '
          f'so they are not counted. Tick Confirmed after checking each one."',

    # ---- next 30 days
    "A4": "Next 30 days of cash",
    "A5": "From", "B5": f"={START}+1", "C5": "to", "D5": f"={START}+{DAYS}",
    "A6": "Starting cash", "B6": f"={CASH0}",
    "C6": f'=IF({CASH_IN}="","Latest statement balances (chequing + savings) on "&TEXT({START},"yyyy-mm-dd")&'
          '". For a forecast from today, enter today\'s date and cash on Bills & Goals.","Entered on Bills & Goals")',
    "A7": "Expected income", "B7": f"=SUM(B{T0}:B{T1})",
    "A8": "Bills paid from cash", "B8": f"=-SUM(C{T0}:C{T1})",
    "A9": "Credit card payment", "B9": f"=-SUM(D{T0}:D{T1})",
    "C9": f'=IFERROR(IF({card(7)}>{START},{CARD_MODE}&", due "&TEXT({card(7)},"yyyy-mm-dd"),'
          '"Latest statement already due before the start date"),"Missing data: no card statement")',
    "A10": "Everyday spending (estimate)", "B10": f"=-SUM(E{T0}:E{T1})",
    "C10": f'="Bills & Goals estimate: "&TEXT({EVERYDAY},"$#,##0")&" a month"',
    "A11": "Projected cash at the end", "B11": f"=INDEX(F{T0}:F{T1},{DAYS})",
    "A12": "Lowest point", "B12": f"=MIN(F{T0}:F{T1})",
    "C12": f'="on "&TEXT(XLOOKUP(MIN(F{T0}:F{T1}),F{T0}:F{T1},A{T0}:A{T1}),"yyyy-mm-dd")',
    "A13": f'=IF(COUNTIFS({TYPE},"Income",{CONF},TRUE)=0,"⚠ No income confirmed on Bills & Goals yet, '
           f'so this forecast has no pay in it",IF(B12<0,"⚠ Cash goes below $0: move money from savings or '
           f'delay a payment","Cash stays above $0 for the next {DAYS} days"))',

    **{f"{c}{T0 - 1}": h for c, h in zip("ABCDEF", ["Date", "Income", "Bills", "Card payment", "Everyday", "Cash"])},
    f"A{T0}": f"=SEQUENCE({DAYS},1,{START}+1)",
    f"B{T0}": f(f'=MAP(A{T0}:A{T1},LAMBDA(d,SUMPRODUCT({occurs("d")}*({TYPE}="Income"),{AMT})))'),
    f"C{T0}": f(f'=MAP(A{T0}:A{T1},LAMBDA(d,SUMPRODUCT({occurs("d")}*({TYPE}="Bill")*({FROM}="Cash"),{AMT})))'),
    # pay the card statement that's already issued; later statements aren't known yet
    f"D{T0}": f'=MAP(A{T0}:A{T1},LAMBDA(d,IFERROR(IF(d={card(7)},'
              f'IF({CARD_MODE}="Minimum",{card(6)},-{card(3)}),0),0)))',
    f"E{T0}": f"=MAP(A{T0}:A{T1},LAMBDA(d,{EVERYDAY}*12/365))",
    f"F{T0}": f"=SCAN({CASH0},SEQUENCE({DAYS}),LAMBDA(acc,i,acc+INDEX(B{T0}:B{T1},i)-INDEX(C{T0}:C{T1},i)"
              f"-INDEX(D{T0}:D{T1},i)-INDEX(E{T0}:E{T1},i)))",

    # ---- recurring commitments
    "H4": "Recurring commitments (confirmed bills)",
    "H5": "Per month", "I5": f'=SUMPRODUCT(({TYPE}="Bill")*({CONF}=TRUE)*{AMT}*{PER_MONTH})',
    "H6": "Per year", "I6": "=I5*12",
    "H7": "Share of income", "I7": f(f'=IFERROR(I5/{INCOME3},"N/A")'),
    "J7": "income = average of the last 3 complete months",
    **{f"{c}9": h for c, h in zip("HIJKLM", ["Name", "Frequency", "Amount", "Per month", "Per year", "Category"])},
    "H10": (f'=ARRAYFORMULA(IFERROR(LET(keep,({TYPE}="Bill")*({CONF}=TRUE)*({NAME}<>""),'
            f'pm,{AMT}*{PER_MONTH},'
            f'SORT(FILTER(HSTACK({NAME},{FREQ},{AMT},pm,pm*12,{BG}$H${R0}:$H${R1}),keep),4,FALSE)),'
            '"No confirmed bills yet: tick Confirmed on Bills & Goals"))'),

    # ---- emergency fund
    "O4": "Emergency fund",
    "O5": f'="Fund: latest balance of "&{EF_ACCT}',
    "P5": f'=IF(COUNTIF(Balances!$A$2:$A,{EF_ACCT})=0,"Missing data",SUMIFS(Balances!$C$2:$C,Balances!$A$2:$A,'
          f'{EF_ACCT},Balances!$B$2:$B,MAXIFS(Balances!$B$2:$B,Balances!$A$2:$A,{EF_ACCT})))',
    "O6": "Essential spending per month (Fixed + Essential, 6-month avg)", "P6": f("=" + ESSENTIAL6),
    "O7": "Months it would cover", "P7": '=IF(ISNUMBER(P5),IF(P6=0,"N/A",P5/P6),"Missing data")',
    "O8": f'="Target: "&{EF_MONTHS}&" months"', "P8": f"={EF_MONTHS}*P6",
    "O9": "Still to save", "P9": '=IF(ISNUMBER(P5),MAX(0,P8-P5),"Missing data")',

    # ---- where the cash stands
    "O12": "Where your cash stands",
    "O13": "Cash (chequing + savings)", "P13": f"={CASH0}",
    "O14": "− Emergency fund", "P14": "=IF(ISNUMBER(P5),-P5,0)",
    "O15": "− Set aside for one-off costs", "P15": f"=-SUM({SINK[3]})",
    "O16": "− Saved for goals", "P16": f"=-SUM({GOAL[3]})",
    "O17": "Unallocated cash", "P17": "=SUM(P13:P16)",
    "O18": '=IF(P17<0,"⚠ More is earmarked than you have in cash","")',

    # ---- credit card
    "O21": "Credit card",
    **{f"O{22 + i}": h for i, h in enumerate([
        "Account", "Statement date", "Balance owed", "Interest rate (purchases)", "Minimum payment",
        "Due date", "Credit limit", "Used of limit"])},
    **{f"P{22 + i}": f'=IFERROR(IF({card(col)}="","Missing data",{sign}{card(col)}),"Missing data")'
       for i, (col, sign) in enumerate([(1, ""), (2, ""), (3, "-"), (9, ""), (6, ""), (7, ""), (8, "")])},
    "P29": '=IFERROR(P24/P28,"Missing data")',

    # ---- one-off costs and goals
    f"A{FUND_ROW - 1}": "One-off costs to save for",
    **{f"{c}{FUND_ROW}": h for c, h in zip("ABCDEFGH", [
        "Name", "Amount", "Due", "Set aside", "Still needed", "Months left", "Per month", "Status"])},
    f"A{FUND_ROW + 1}": _plan_table(SINK),
    f"A{GOAL_ROW - 1}": "Savings goals",
    **{f"{c}{GOAL_ROW}": h for c, h in zip("ABCDEFGH", [
        "Name", "Target", "Target date", "Saved so far", "Still needed", "Months left", "Per month", "Status"])},
    f"A{GOAL_ROW + 1}": _plan_table(GOAL),

    # ---- can I afford my plans?
    f"A{AFFORD_ROW}": "Can I afford my plans?",
    f"A{AFFORD_ROW + 1}": "Needed per month: one-off costs", f"B{AFFORD_ROW + 1}": f"=SUM(G{FUND_ROW + 1}:G{FUND_ROW + 16})",
    f"A{AFFORD_ROW + 2}": "Needed per month: savings goals", f"B{AFFORD_ROW + 2}": f"=SUM(G{GOAL_ROW + 1}:G{GOAL_ROW + 9})",
    f"A{AFFORD_ROW + 3}": "Emergency fund top-up (spread over 12 months)",
    f"B{AFFORD_ROW + 3}": "=IF(ISNUMBER(P9),P9/12,0)",
    f"A{AFFORD_ROW + 4}": "Total needed per month", f"B{AFFORD_ROW + 4}": f"=SUM(B{AFFORD_ROW + 1}:B{AFFORD_ROW + 3})",
    f"A{AFFORD_ROW + 5}": "Your savings goal (Plan tab)", f"B{AFFORD_ROW + 5}": "=Plan!$B$4",
    f"A{AFFORD_ROW + 6}": "What you actually saved per month (last 3 complete months)",
    f"B{AFFORD_ROW + 6}": f(f"={INCOME3}-{SPEND3}"),
    f"A{AFFORD_ROW + 7}": (f'=IF(B{AFFORD_ROW + 4}=0,"No plans entered yet",IF(B{AFFORD_ROW + 6}>=B{AFFORD_ROW + 4},'
                           f'"Your recent saving covers these plans","⚠ These plans need "&TEXT(B{AFFORD_ROW + 4}'
                           f'-B{AFFORD_ROW + 6},"$#,##0")&" a month more than you have been saving"))'),
}
CELLS = {k: f(v) for k, v in CELLS.items()}


def build(sh: gspread.Spreadsheet, suggestions: list[Suggestion], start: date) -> bool:
    """Create Bills & Goals once (pre-filled with unconfirmed suggestions) and rebuild Outlook.
    Returns True if Bills & Goals was created."""
    created = _ensure_inputs(sh, suggestions, start)
    ws = _recreate(sh, OUTLOOK, 2)
    ws.batch_update([{"range": k, "values": [[v]]} for k, v in CELLS.items()], value_input_option="USER_ENTERED")
    o = ws.id
    bold = {"textFormat": {"bold": True}}
    section = {"textFormat": {"bold": True, "fontSize": 12}}
    a = AFFORD_ROW
    sh.batch_update({"requests": [
        _fmt(o, "A1", {"textFormat": {"bold": True, "fontSize": 16}}, "textFormat"),
        _fmt(o, "A2", {"textFormat": {"italic": True, "foregroundColor": _rgb(INK_MUTED)}}, "textFormat"),
        _fmt(o, "C6:C10", {"textFormat": {"italic": True, "foregroundColor": _rgb(INK_MUTED)}}, "textFormat"),
        _fmt(o, "J7", {"textFormat": {"italic": True, "foregroundColor": _rgb(INK_MUTED)}}, "textFormat"),
        *[_fmt(o, r, section, "textFormat") for r in (
            "A4", "H4", "O4", "O12", "O21", f"A{FUND_ROW - 1}", f"A{GOAL_ROW - 1}", f"A{a}")],
        *[_fmt(o, r, bold, "textFormat") for r in (
            "A11:B12", "A13", f"A{T0 - 1}:F{T0 - 1}", "H5:I6", "H9:M9", "O7:P7", "O17:P17",
            f"A{FUND_ROW}:H{FUND_ROW}", f"A{GOAL_ROW}:H{GOAL_ROW}", f"A{a + 4}:B{a + 4}", f"A{a + 7}")],
        _fmt(o, "B5:D5", {"numberFormat": DAY_FMT}, "numberFormat"),
        _fmt(o, "B6:B12", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(o, f"A{T0}:A{T1}", {"numberFormat": {"type": "DATE", "pattern": "ddd mmm d"}}, "numberFormat"),
        _fmt(o, f"B{T0}:F{T1}", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(o, "I5:I6", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(o, "I7", {"numberFormat": PCT}, "numberFormat"),
        _fmt(o, "J10:L40", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(o, "P5:P6", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(o, "P7", {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}}, "numberFormat"),
        _fmt(o, "P8:P9", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(o, "P13:P17", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(o, "P23", {"numberFormat": DAY_FMT}, "numberFormat"),
        _fmt(o, "P24", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(o, "P25", {"numberFormat": {"type": "PERCENT", "pattern": "0.00%"}}, "numberFormat"),
        _fmt(o, "P26", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(o, "P27", {"numberFormat": DAY_FMT}, "numberFormat"),
        _fmt(o, "P28", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(o, "P29", {"numberFormat": PCT}, "numberFormat"),
        *[_fmt(o, f"{c}{r0}:{c}{r0 + 15}", {"numberFormat": fmt}, "numberFormat")
          for r0 in (FUND_ROW + 1, GOAL_ROW + 1)
          for c, fmt in (("B", MONEY), ("C", DAY_FMT), ("D", MONEY), ("E", MONEY), ("G", MONEY))],
        _fmt(o, f"B{a + 1}:B{a + 6}", {"numberFormat": MONEY}, "numberFormat"),
        _when(o, f"F{T0}:F{T1}", f"=F{T0}<0", OVER_BG),
        _when(o, "A13", '=LEFT($A$13,1)="⚠"', OVER_BG),
        _when(o, "A13", '=$A$13<>""', GOOD_BG, 1),
        _when(o, "O18", '=$O$18<>""', OVER_BG),
        _when(o, "P5:P29", '=P5="Missing data"', WARN_BG),
        _when(o, f"H{FUND_ROW + 1}:H{GOAL_ROW + 10}", f'=H{FUND_ROW + 1}="Missing data"', WARN_BG),
        _when(o, f"A{a + 7}", f'=LEFT($A${a + 7},1)="⚠"', WARN_BG),
        _when(o, "A2", '=LEFT($A$2,1)<>"0"', WARN_BG),
        _width(o, 0, 0, 230), _width(o, 1, 5, 105), _width(o, 6, 6, 30), _width(o, 7, 7, 200),
        _width(o, 8, 12, 95), _width(o, 13, 13, 30), _width(o, 14, 14, 330), _width(o, 15, 15, 130),
        _chart(o, o, f"Projected cash, next {DAYS} days", "LINE", f"A{T0 - 1}:A{T1}",
               [(f"F{T0 - 1}:F{T1}", BLUE)], 30, 7),
    ]})
    return created


def _ensure_inputs(sh: gspread.Spreadsheet, suggestions: list[Suggestion], start: date) -> bool:
    try:
        sh.worksheet(INPUTS)
        return False
    except gspread.WorksheetNotFound:
        pass
    ws = sh.add_worksheet(INPUTS, rows=60, cols=16, index=3)
    last = calendar.monthrange(start.year, start.month)[1]
    recurring = [[s.name, s.kind, float(s.amount), "Monthly",
                  date(start.year, start.month, min(s.day, last)).isoformat(), s.paid_from, False, s.category,
                  f"Suggested from history (seen in {s.months_seen} of the last 6 months). Check, then tick Confirmed."]
                 for s in suggestions][:R1 - R0 + 1]
    everyday = ";".join(f'"{c}"' for c in EVERYDAY_CATEGORIES)
    values = {
        "A1": [["Bills & Goals"]],
        "A2": [["Your inputs for the Outlook tab. Edit the yellow cells; the program never overwrites this tab."]],
        "A4": [["Cash-flow start date", "=MAXIFS(Balances!$B$2:$B,Balances!$E$2:$E,\"deposit\")",
                "Default: date of your latest bank statement. Type today's date to forecast from today."]],
        "A5": [["Starting cash (blank = latest statement balances)", "",
                "If you set today's date above, enter today's chequing + savings total here."]],
        "A6": [["Everyday spending per month (things not listed as bills)",
                f"=SUMPRODUCT(ISNUMBER(MATCH(Plan!$A$10:$A$60,{{{everyday}}},0))*Plan!$C$10:$C$60)",
                "Default: your Plan budgets for " + ", ".join(EVERYDAY_CATEGORIES) + "."]],
        "A7": [["Credit card: pay each statement's", "Full balance", "Full balance or Minimum"]],
        "A8": [["Emergency fund account", "RBC Saving ...9849", "Which account holds your emergency money"]],
        "A9": [["Emergency fund target (months of essential spending)", 3, ""]],
        "A11": [["Recurring income & bills"]],
        "A12": [["Name", "Type", "Amount", "Frequency", "Date", "Paid from", "Confirmed", "Category", "Note"]],
        **({"A13": recurring} if recurring else {}),
        "K11": [["One-off costs to save for (annual fees, travel, dentist…)"]],
        "K12": [["Name", "Amount", "Due date", "Set aside so far", "Note"]],
        "K30": [["Savings goals"]],
        "K31": [["Name", "Target", "Target date", "Saved so far", "Note"]],
        "K42": [["Date: for Monthly, the day of the month is what counts; for Biweekly/Weekly, any one "
                 "pay or bill date; for Yearly/Once, the exact date. Paid from 'Card' bills reach your "
                 "cash through the card payment, so they aren't counted twice."]],
    }
    ws.batch_update([{"range": k, "values": v} for k, v in values.items()], value_input_option="USER_ENTERED")
    w = ws.id

    def dropdown(a1, options=None, rng=None, checkbox=False):
        cond = ({"type": "BOOLEAN"} if checkbox else
                {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": o} for o in options]} if options else
                {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": rng}]})
        return {"setDataValidation": {"range": _grid(w, a1), "rule": {"condition": cond, "showCustomUi": True,
                                                                     "strict": not rng}}}
    sh.batch_update({"requests": [
        _fmt(w, "A1", {"textFormat": {"bold": True, "fontSize": 16}}, "textFormat"),
        _fmt(w, "A2", {"textFormat": {"italic": True, "foregroundColor": _rgb(INK_MUTED)}}, "textFormat"),
        _fmt(w, "C4:C9", {"textFormat": {"italic": True, "foregroundColor": _rgb(INK_MUTED)}}, "textFormat"),
        _fmt(w, "K42", {"textFormat": {"italic": True, "foregroundColor": _rgb(INK_MUTED)}}, "textFormat"),
        *[_fmt(w, r, {"textFormat": {"bold": True, "fontSize": 12}}, "textFormat") for r in ("A11", "K11", "K30")],
        *[_fmt(w, r, {"textFormat": {"bold": True}}, "textFormat") for r in ("A12:I12", "K12:O12", "K31:O31")],
        _fmt(w, "B4:B9", {"backgroundColor": _rgb(INPUT_BG)}, "backgroundColor"),
        _fmt(w, "B4", {"numberFormat": DAY_FMT}, "numberFormat"),
        _fmt(w, "B5:B6", {"numberFormat": MONEY}, "numberFormat"),
        _fmt(w, f"A{R0}:H{R1}", {"backgroundColor": _rgb(INPUT_BG)}, "backgroundColor"),
        _fmt(w, f"C{R0}:C{R1}", {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"}}, "numberFormat"),
        _fmt(w, f"E{R0}:E{R1}", {"numberFormat": DAY_FMT}, "numberFormat"),
        _fmt(w, f"I{R0}:I{R1}", {"textFormat": {"italic": True, "foregroundColor": _rgb(INK_MUTED)}}, "textFormat"),
        *[_fmt(w, f"K{r0}:N{r1}", {"backgroundColor": _rgb(INPUT_BG)}, "backgroundColor")
          for r0, r1 in ((S0, S1), (G0, G1))],
        *[_fmt(w, f"{c}{r0}:{c}{r1}", {"numberFormat": fmt}, "numberFormat")
          for r0, r1 in ((S0, S1), (G0, G1)) for c, fmt in (("L", MONEY), ("M", DAY_FMT), ("N", MONEY))],
        dropdown("B7", ["Full balance", "Minimum"]),
        dropdown("B8", rng="=Balances!$A$2:$A"),
        dropdown(f"B{R0}:B{R1}", ["Income", "Bill"]),
        dropdown(f"D{R0}:D{R1}", FREQUENCIES),
        dropdown(f"F{R0}:F{R1}", ["Cash", "Card"]),
        dropdown(f"G{R0}:G{R1}", checkbox=True),
        dropdown(f"H{R0}:H{R1}", rng="=Categories!$A$2:$A"),
        _width(w, 0, 0, 330), _width(w, 1, 1, 120), _width(w, 2, 7, 100), _width(w, 8, 8, 330),
        _width(w, 9, 9, 30), _width(w, 10, 10, 230), _width(w, 11, 14, 110),
    ]})
    return True
