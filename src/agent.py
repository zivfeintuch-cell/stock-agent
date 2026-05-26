"""
stock_agent — src/agent.py
"""
import os, json, datetime
import anthropic, gspread, requests
from google.oauth2.service_account import Credentials

ANTHROPIC_API_KEY       = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_SHEET_ID         = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
TELEGRAM_BOT_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID        = os.environ.get("TELEGRAM_CHAT_ID", "")
WATCHLIST_TAB           = os.environ.get("WATCHLIST_TAB", "watchlist")
HISTORY_TAB             = os.environ.get("HISTORY_TAB", "history")
UNIVERSE_TAB            = os.environ.get("UNIVERSE_TAB", "universe")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

UNIVERSE_TICKERS = ["UNH","ORCL","MSFT","SPY","AMZN","NVDA","TTD","MP","ONDS","INVZ"]

UNIVERSE_HEADERS = [
    "ticker","name","price","eps_ttm","pe","forward_pe",
    "revenue_growth_yoy","operating_margin","fcf_margin",
    "net_debt_ebitda","last_updated"
]

UNIVERSE_SYSTEM = """You are a financial data extraction agent.
Find the most recent official values for all requested metrics.
Return ONLY valid JSON (no markdown, no explanation):
{
  "name": "<company full name>",
  "price": <float or null>,
  "eps_ttm": <float or null>,
  "pe": <float or null>,
  "forward_pe": <float or null>,
  "revenue_growth_yoy": <float percentage e.g. 12.5 for 12.5% or null>,
  "operating_margin": <float percentage e.g. 18.3 for 18.3% or null>,
  "fcf_margin": <float percentage or null>,
  "net_debt_ebitda": <float or null>,
  "source": "<url of primary source used>"
}"""


def sheets_client():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS_JSON), scopes=SCOPES)
    return gspread.authorize(creds)


def ensure_universe_tab(gc):
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(UNIVERSE_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(UNIVERSE_TAB, rows=50, cols=len(UNIVERSE_HEADERS))
        ws.append_row(UNIVERSE_HEADERS)
        print(f"[universe] created tab '{UNIVERSE_TAB}'")
    return ws


def load_watchlist(gc):
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    print(f"Sheet title: {sh.title}")
    print(f"Worksheets: {[w.title for w in sh.worksheets()]}")
    ws = sh.worksheet(WATCHLIST_TAB)
    return [r for r in ws.get_all_records() if r.get("ticker")]


def append_history(gc, rows):
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(HISTORY_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(HISTORY_TAB, rows=2000, cols=10)
        ws.append_row(["date","ticker","metric","value","low","high","breached","note","source"])
    today = datetime.date.today().isoformat()
    for r in rows:
        ws.append_row([today, r["ticker"], r["metric_name"],
            r.get("fetched_value",""), r.get("threshold_low",""), r.get("threshold_high",""),
            "YES" if is_breached(r) else "no",
            r.get("agent_note",""), r.get("source","")])


def write_last_value(gc, rows):
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    ws = sh.worksheet(WATCHLIST_TAB)
    hdr = ws.row_values(1)
    col_val = hdr.index("last_value") + 1
    col_upd = hdr.index("last_updated") + 1
    all_rows = ws.get_all_records()
    today = datetime.date.today().isoformat()
    for row in rows:
        if row.get("fetched_value") is None:
            continue
        for i, sr in enumerate(all_rows):
            if sr["ticker"] == row["ticker"] and sr["metric_name"] == row["metric_name"]:
                ws.update_cell(i+2, col_val, row["fetched_value"])
                ws.update_cell(i+2, col_upd, today)
                break


def fetch_universe_metric(ticker):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-sonnet-4-5", max_tokens=1024, system=UNIVERSE_SYSTEM,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role":"user","content":
            f"Ticker: {ticker}\n"
            f"Fetch: price, EPS (TTM), P/E, Forward P/E, Revenue Growth YoY %, "
            f"Operating Margin %, Free Cash Flow Margin %, Net Debt/EBITDA.\n"
            f"Return only JSON."}])
    text = next((b.text for b in resp.content if b.type=="text"), "")
    try:
        return json.loads(text.replace("```json","").replace("```","").strip())
    except json.JSONDecodeError:
        print(f"  [universe] JSON parse error for {ticker}: {text[:200]}")
        return {}


def update_universe(gc):
    ws = ensure_universe_tab(gc)
    existing = ws.get_all_records()
    ticker_to_row = {r["ticker"]: i+2 for i, r in enumerate(existing)}
    today = datetime.date.today().isoformat()

    print(f"Updating universe ({len(UNIVERSE_TICKERS)} tickers)...")
    for ticker in UNIVERSE_TICKERS:
        print(f"  fetching {ticker}...")
        data = fetch_universe_metric(ticker)
        row_data = [
            ticker,
            data.get("name", ""),
            data.get("price", ""),
            data.get("eps_ttm", ""),
            data.get("pe", ""),
            data.get("forward_pe", ""),
            data.get("revenue_growth_yoy", ""),
            data.get("operating_margin", ""),
            data.get("fcf_margin", ""),
            data.get("net_debt_ebitda", ""),
            today
        ]
        if ticker in ticker_to_row:
            row_num = ticker_to_row[ticker]
            ws.update(f"A{row_num}:K{row_num}", [row_data])
        else:
            ws.append_row(row_data)
        print(f"    price={data.get('price')} eps={data.get('eps_ttm')} pe={data.get('pe')}")

    print("Universe update complete.")


SYSTEM = """You are a financial data extraction agent.
Find the most recent official value for the given metric.
Return ONLY valid JSON (no markdown):
{"value": <float or null>, "period": "<Q1 2025>", "source": "<url>", "note": "<one sentence>"}"""


def fetch_metric(ticker, metric, description, source_hint):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-sonnet-4-5", max_tokens=512, system=SYSTEM,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role":"user","content":
            f"Ticker:{ticker}\nMetric:{metric}\nDesc:{description}\nHint:{source_hint}\nReturn only JSON."}])
    text = next((b.text for b in resp.content if b.type=="text"), "")
    try:
        return json.loads(text.replace("```json","").replace("```","").strip())
    except json.JSONDecodeError:
        return {"value":None,"period":"","source":"","note":text[:200]}


def is_breached(row):
    val = row.get("fetched_value")
    if val is None: return False
    try:
        v = float(val)
        lo, hi = row.get("threshold_low"), row.get("threshold_high")
        if lo not in ("",None) and v < float(lo): return True
        if hi not in ("",None) and v > float(hi): return True
    except (ValueError, TypeError): pass
    return False


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] not configured"); return
    r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id":TELEGRAM_CHAT_ID,"text":text,"parse_mode":"HTML"}, timeout=10)
    if not r.ok: print(f"[Telegram] error: {r.text}")


def format_alert(row):
    return (f"<b>Alert: {row['ticker']} — {row['metric_name']}</b>\n"
            f"Value: <code>{row.get('fetched_value')}</code>\n"
            f"Range: [{row.get('threshold_low','—')}, {row.get('threshold_high','—')}]\n"
            f"Note:  {row.get('agent_note','')}")


def is_due(row):
    freq = str(row.get("frequency","")).lower()
    today = datetime.date.today()
    return (freq=="daily" or
            (freq=="weekly" and today.weekday()==0) or
            (freq in ("monthly","quarterly") and today.day==1))


def is_universe_day():
    """Run universe update every Sunday (weekday 6)"""
    return True


def run():
    print(f"[{datetime.datetime.now().isoformat()}] agent starting")
    gc = sheets_client()

    # Universe update — every Sunday
    if is_universe_day():
        update_universe(gc)

    # Watchlist alerts — per frequency setting
    to_check = [r for r in load_watchlist(gc) if is_due(r)]
    if not to_check:
        print("Nothing due today")
    else:
        print(f"Checking {len(to_check)} metric(s)...")
        results, alerts = [], []
        for row in to_check:
            print(f"  {row['ticker']} / {row['metric_name']}")
            data = fetch_metric(row["ticker"], row["metric_name"],
                                row.get("metric_description",""), row.get("source_hint",""))
            fetched = data.get("value")
            if fetched is None and row.get("last_value") not in ("", None):
                fetched = float(row["last_value"])
                data["note"] = "[fallback to last_value] " + data.get("note","")
            row.update({"fetched_value": fetched, "period": data.get("period",""),
                        "source": data.get("source",""), "agent_note": data.get("note","")})
            results.append(row)
            print(f"    → {row['fetched_value']}  [{'BREACH' if is_breached(row) else 'ok'}]")
            if is_breached(row): alerts.append(row)
        append_history(gc, results)
        write_last_value(gc, results)
        if alerts:
            msg = f"<b>Stock Agent — {len(alerts)} alert(s) — {datetime.date.today()}</b>\n\n"
            msg += "\n\n".join(format_alert(r) for r in alerts)
            send_telegram(msg)
        print(f"Done. {len(alerts)} alert(s) sent.")


if __name__ == "__main__":
    run()
