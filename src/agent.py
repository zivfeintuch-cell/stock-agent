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
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def sheets_client():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS_JSON), scopes=SCOPES)
    return gspread.authorize(creds)

def load_watchlist(gc):
    ws = gc.open_by_key(GOOGLE_SHEET_ID).worksheet(WATCHLIST_TAB)
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

SYSTEM = """You are a financial data extraction agent.
Find the most recent official value for the given metric.
Return ONLY valid JSON (no markdown):
{"value": <float or null>, "period": "<Q1 2025>", "source": "<url>", "note": "<one sentence>"}"""

def fetch_metric(ticker, metric, description, source_hint):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-sonnet-4-20250514", max_tokens=512, system=SYSTEM,
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

def run():
    print(f"[{datetime.datetime.now().isoformat()}] agent starting")
    gc = sheets_client()
    to_check = [r for r in load_watchlist(gc) if is_due(r)]
    if not to_check:
        print("Nothing due today"); return
    print(f"Checking {len(to_check)} metric(s)...")
    results, alerts = [], []
    for row in to_check:
        print(f"  {row['ticker']} / {row['metric_name']}")
        data = fetch_metric(row["ticker"], row["metric_name"],
                            row.get("metric_description",""), row.get("source_hint",""))
        row.update({"fetched_value":data.get("value"),"period":data.get("period",""),
                    "source":data.get("source",""),"agent_note":data.get("note","")})
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
