"""
stock_agent — src/agent.py
Universe update via yfinance (free, no API key needed).
Watchlist alerts via Claude + web search.
"""
import os, json, datetime, time, re
import anthropic, gspread, requests
import yfinance as yf
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


# ── Google Sheets ──────────────────────────────────────────────────────────────

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
        ws.update(values=[UNIVERSE_HEADERS], range_name="A1")
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


# ── Universe (yfinance) ────────────────────────────────────────────────────────

def safe_pct(val):
    """yfinance returns ratios (0.17), convert to percentage (17.0)."""
    if val is None:
        return None
    try:
        return round(float(val) * 100, 2)
    except (TypeError, ValueError):
        return None


def safe_float(val, decimals=2):
    if val is None:
        return None
    try:
        return round(float(val), decimals)
    except (TypeError, ValueError):
        return None


def calc_net_debt_ebitda(info):
    """Net debt / EBITDA — calculated from yfinance fields."""
    try:
        total_debt = float(info.get("totalDebt") or 0)
        cash       = float(info.get("totalCash") or 0)
        ebitda     = float(info.get("ebitda") or 0)
        if ebitda == 0:
            return None
        return round((total_debt - cash) / ebitda, 2)
    except (TypeError, ValueError):
        return None


def calc_fcf_margin(info):
    """FCF margin = freeCashflow / totalRevenue."""
    try:
        fcf     = float(info.get("freeCashflow") or 0)
        revenue = float(info.get("totalRevenue") or 0)
        if revenue == 0:
            return None
        return round((fcf / revenue) * 100, 2)
    except (TypeError, ValueError):
        return None


def fetch_universe_yfinance(ticker):
    """Fetch all universe metrics for one ticker via yfinance."""
    try:
        t    = yf.Ticker(ticker)
        info = t.info

        # For ETFs like SPY, use navPrice or regularMarketPrice
        price = (info.get("currentPrice")
                 or info.get("navPrice")
                 or info.get("regularMarketPrice"))

        data = {
            "name":                info.get("shortName") or info.get("longName"),
            "price":               safe_float(price),
            "eps_ttm":             safe_float(info.get("trailingEps")),
            "pe":                  safe_float(info.get("trailingPE"), 1),
            "forward_pe":          safe_float(info.get("forwardPE"), 1),
            "revenue_growth_yoy":  safe_pct(info.get("revenueGrowth")),
            "operating_margin":    safe_pct(info.get("operatingMargins")),
            "fcf_margin":          calc_fcf_margin(info),
            "net_debt_ebitda":     calc_net_debt_ebitda(info),
        }
        return data

    except Exception as e:
        print(f"  [yfinance] error for {ticker}: {e}")
        return {}


def update_universe(gc):
    ws = ensure_universe_tab(gc)
    existing       = ws.get_all_records()
    ticker_to_row  = {r["ticker"]: i+2 for i, r in enumerate(existing)}
    today          = datetime.date.today().isoformat()

    print(f"Updating universe ({len(UNIVERSE_TICKERS)} tickers) via yfinance...")
    for ticker in UNIVERSE_TICKERS:
        print(f"  fetching {ticker}...")
        data = fetch_universe_yfinance(ticker)

        row_data = [
            ticker,
            data.get("name") or "",
            data.get("price") or "",
            data.get("eps_ttm") or "",
            data.get("pe") or "",
            data.get("forward_pe") or "",
            data.get("revenue_growth_yoy") or "",
            data.get("operating_margin") or "",
            data.get("fcf_margin") or "",
            data.get("net_debt_ebitda") or "",
            today,
        ]

        if ticker in ticker_to_row:
            row_num = ticker_to_row[ticker]
            ws.update(values=[row_data], range_name=f"A{row_num}:K{row_num}")
        else:
            ws.append_row(row_data)

        print(f"    price={data.get('price')}  pe={data.get('pe')}  "
              f"fwd_pe={data.get('forward_pe')}  rev_growth={data.get('revenue_growth_yoy')}%  "
              f"op_margin={data.get('operating_margin')}%  fcf={data.get('fcf_margin')}%  "
              f"nd_ebitda={data.get('net_debt_ebitda')}")

        time.sleep(2)   # courtesy delay — yfinance doesn't need 60s

    print("Universe update complete.")


# ── Watchlist alerts (Claude + web search) ────────────────────────────────────

SYSTEM = """You are a financial data extraction agent.
Find the most recent official value for the given metric.
Return ONLY a raw JSON object, no markdown, no backticks:
{"value": <float or null>, "period": "<Q1 2025>", "source": "<url>", "note": "<one sentence>"}"""


def extract_json(text):
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {}


def fetch_metric(ticker, metric, description, source_hint):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=SYSTEM,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        tool_choice={"type": "auto", "disable_parallel_tool_use": True},
        messages=[{"role": "user", "content":
            f"Ticker:{ticker}\nMetric:{metric}\nDesc:{description}\nHint:{source_hint}\nReturn only raw JSON."}])
    text = next((b.text for b in resp.content if b.type == "text"), "")
    data = extract_json(text)
    if not data:
        return {"value": None, "period": "", "source": "", "note": text[:200]}
    return data


def is_breached(row):
    val = row.get("fetched_value")
    if val is None:
        return False
    try:
        v  = float(val)
        lo = row.get("threshold_low")
        hi = row.get("threshold_high")
        if lo not in ("", None) and v < float(lo):
            return True
        if hi not in ("", None) and v > float(hi):
            return True
    except (TypeError, ValueError):
        pass
    return False


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] not configured")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10)
    if not r.ok:
        print(f"[Telegram] error: {r.text}")


def format_alert(row):
    return (f"<b>Alert: {row['ticker']} — {row['metric_name']}</b>\n"
            f"Value: <code>{row.get('fetched_value')}</code>\n"
            f"Range: [{row.get('threshold_low','—')}, {row.get('threshold_high','—')}]\n"
            f"Note:  {row.get('agent_note','')}")


def is_due(row):
    freq  = str(row.get("frequency", "")).lower()
    today = datetime.date.today()
    return (freq == "daily"
            or (freq == "weekly"   and today.weekday() == 0)
            or (freq in ("monthly","quarterly") and today.day == 1))


def is_universe_day():
    return datetime.date.today().weekday() == 6   # Sunday only


# ── Main ───────────────────────────────────────────────────────────────────────

def run():
    print(f"[{datetime.datetime.now().isoformat()}] agent starting")
    gc = sheets_client()

    if is_universe_day():
        update_universe(gc)

    to_check = [r for r in load_watchlist(gc) if is_due(r)]
    if not to_check:
        print("Nothing due today")
    else:
        print(f"Checking {len(to_check)} metric(s)...")
        results, alerts = [], []
        for row in to_check:
            print(f"  {row['ticker']} / {row['metric_name']}")
            data = fetch_metric(
                row["ticker"], row["metric_name"],
                row.get("metric_description",""), row.get("source_hint",""))
            fetched = data.get("value")
            if fetched is None and row.get("last_value") not in ("", None):
                fetched    = float(row["last_value"])
                data["note"] = "[fallback to last_value] " + data.get("note","")
            row.update({
                "fetched_value": fetched,
                "period":        data.get("period",""),
                "source":        data.get("source",""),
                "agent_note":    data.get("note",""),
            })
            results.append(row)
            print(f"    → {row['fetched_value']}  [{'BREACH' if is_breached(row) else 'ok'}]")
            if is_breached(row):
                alerts.append(row)

        append_history(gc, results)
        write_last_value(gc, results)

        if alerts:
            msg  = f"<b>Stock Agent — {len(alerts)} alert(s) — {datetime.date.today()}</b>\n\n"
            msg += "\n\n".join(format_alert(r) for r in alerts)
            send_telegram(msg)

        print(f"Done. {len(alerts)} alert(s) sent.")


if __name__ == "__main__":
    run()
