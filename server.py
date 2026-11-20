"""MarktRadar server: Yahoo Finance data, background refresh, forecasts and live track record.

Python standard library only. Run `python server.py` (add `--open` to open the browser).
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

import model

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
DB = ROOT / "marktradar.sqlite3"
REFRESH_SECONDS = 300
FULL_HISTORY_SECONDS = 6 * 3600
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MarktRadar/2.1"

# cat: us | eu | index | commodity | crypto ;  unit is shown after the price (e.g. per ounce)
def _sym(name, exchange, color, mark, cat, cur="USD", unit=""):
    return {"name": name, "exchange": exchange, "color": color, "mark": mark, "cat": cat, "currency": cur, "unit": unit}


SYMBOLS = {
    # US stocks
    "NVDA": _sym("NVIDIA", "NASDAQ", "#76b900", "N", "us"),
    "MSFT": _sym("Microsoft", "NASDAQ", "#2478c3", "M", "us"),
    "AAPL": _sym("Apple", "NASDAQ", "#556068", "A", "us"),
    "AMZN": _sym("Amazon", "NASDAQ", "#f19a38", "a", "us"),
    "GOOGL": _sym("Alphabet (Google)", "NASDAQ", "#4285f4", "G", "us"),
    "META": _sym("Meta (Facebook)", "NASDAQ", "#0866ff", "M", "us"),
    "TSLA": _sym("Tesla", "NASDAQ", "#e82127", "T", "us"),
    "AVGO": _sym("Broadcom", "NASDAQ", "#c8323e", "B", "us"),
    "AMD": _sym("AMD", "NASDAQ", "#2c8a6e", "AMD", "us"),
    "NFLX": _sym("Netflix", "NASDAQ", "#b20710", "N", "us"),
    "PLTR": _sym("Palantir", "NASDAQ", "#3a3f44", "P", "us"),
    "COST": _sym("Costco", "NASDAQ", "#005daa", "C", "us"),
    "BRK-B": _sym("Berkshire Hathaway", "NYSE", "#2f5a48", "B", "us"),
    "JPM": _sym("JPMorgan Chase", "NYSE", "#6b4f34", "J", "us"),
    "V": _sym("Visa", "NYSE", "#1a1f71", "V", "us"),
    "LLY": _sym("Eli Lilly", "NYSE", "#d52b1e", "L", "us"),
    "WMT": _sym("Walmart", "NYSE", "#0071ce", "W", "us"),
    "KO": _sym("Coca-Cola", "NYSE", "#e41e2b", "KO", "us"),
    "XOM": _sym("ExxonMobil", "NYSE", "#ed1b2d", "X", "us"),
    "DIS": _sym("Disney", "NYSE", "#113ccf", "D", "us"),
    # Europe & Austria
    "SAP.DE": _sym("SAP", "XETRA", "#0a6ed1", "SAP", "eu", "EUR"),
    "SIE.DE": _sym("Siemens", "XETRA", "#009999", "S", "eu", "EUR"),
    "ALV.DE": _sym("Allianz", "XETRA", "#003781", "A", "eu", "EUR"),
    "MBG.DE": _sym("Mercedes-Benz", "XETRA", "#5f6b73", "M", "eu", "EUR"),
    "ASML.AS": _sym("ASML", "Amsterdam", "#10069f", "AS", "eu", "EUR"),
    "MC.PA": _sym("LVMH", "Paris", "#8a6d3b", "LV", "eu", "EUR"),
    "OMV.VI": _sym("OMV", "Wien", "#0b3d91", "O", "eu", "EUR"),
    "EBS.VI": _sym("Erste Group", "Wien", "#2870ed", "E", "eu", "EUR"),
    "VOE.VI": _sym("voestalpine", "Wien", "#00508c", "V", "eu", "EUR"),
    # Indices
    "^GSPC": _sym("S&P 500", "USA", "#3d4b57", "500", "index", "USD", "Punkte"),
    "^NDX": _sym("Nasdaq 100", "USA", "#3d4b57", "100", "index", "USD", "Punkte"),
    "^GDAXI": _sym("DAX", "Deutschland", "#3d4b57", "DAX", "index", "EUR", "Punkte"),
    "^ATX": _sym("ATX", "Österreich", "#3d4b57", "ATX", "index", "EUR", "Punkte"),
    "^STOXX50E": _sym("Euro Stoxx 50", "Eurozone", "#3d4b57", "50", "index", "EUR", "Punkte"),
    # Commodities (front-month futures)
    "GC=F": _sym("Gold", "Rohstoff", "#c9a227", "Au", "commodity", "USD", "je Unze"),
    "SI=F": _sym("Silber", "Rohstoff", "#9aa4ad", "Ag", "commodity", "USD", "je Unze"),
    "BZ=F": _sym("Öl (Brent)", "Rohstoff", "#3b3b3b", "Öl", "commodity", "USD", "je Barrel"),
    "CL=F": _sym("Öl (WTI)", "Rohstoff", "#4a4a4a", "WTI", "commodity", "USD", "je Barrel"),
    "NG=F": _sym("Erdgas", "Rohstoff", "#3f7fbf", "Gas", "commodity", "USD", "je MMBtu"),
    "HG=F": _sym("Kupfer", "Rohstoff", "#b87333", "Cu", "commodity", "USD", "je Pfund"),
    # Crypto & currencies
    "BTC-USD": _sym("Bitcoin", "Krypto", "#f7931a", "₿", "crypto"),
    "ETH-USD": _sym("Ethereum", "Krypto", "#627eea", "Ξ", "crypto"),
    "EURUSD=X": _sym("Euro in Dollar", "Devisen", "#1f4e9c", "€$", "crypto", "USD", "pro Euro"),
}
MARKET = ["SPY", "^VIX"]

# ------------------------------------------------------------------ news sentiment
# Weighted lexicon with phrases and simple negation. Still crude, but far less
# noisy than counting single words ("cut" in "price cut" vs "job cuts" etc.).
_POS = {
    "beat": 2, "beats": 2, "tops": 2, "surpass": 2, "surpasses": 2, "record": 1.5, "upgrade": 2, "upgraded": 2,
    "outperform": 2, "raises": 1.5, "raised": 1.5, "boost": 1, "boosts": 1, "surge": 2, "surges": 2, "soar": 2,
    "soars": 2, "jump": 1.5, "jumps": 1.5, "rally": 1.5, "rallies": 1.5, "gain": 1, "gains": 1, "growth": 1,
    "strong": 1, "stronger": 1, "profit": 1, "profitable": 1, "wins": 1.5, "win": 1, "approval": 1.5,
    "approved": 1.5, "launch": 0.5, "launches": 0.5, "partnership": 1, "deal": 0.5, "demand": 0.5,
    "bullish": 2, "buy": 1, "optimistic": 1.5, "rebound": 1.5, "rebounds": 1.5, "expands": 1, "accelerates": 1,
    "steigt": 1.5, "gewinn": 1, "wachstum": 1, "rekord": 1.5, "stark": 1, "erhöht": 1, "übertrifft": 2,
}
_NEG = {
    "miss": 2, "misses": 2, "missed": 2, "downgrade": 2, "downgraded": 2, "underperform": 2, "cut": 1, "cuts": 1,
    "slash": 1.5, "slashes": 1.5, "fall": 1, "falls": 1.5, "drop": 1, "drops": 1.5, "plunge": 2.5, "plunges": 2.5,
    "tumble": 2, "tumbles": 2, "sink": 1.5, "sinks": 1.5, "slide": 1, "slides": 1, "loss": 1.5, "losses": 1.5,
    "lawsuit": 1.5, "sued": 1.5, "probe": 1.5, "investigation": 1.5, "antitrust": 1, "fine": 1, "fined": 1.5,
    "weak": 1.5, "weaker": 1.5, "warn": 1.5, "warns": 1.5, "warning": 1.5, "risk": 0.5, "risks": 0.5,
    "decline": 1, "declines": 1.5, "sell": 1, "selloff": 2, "sell-off": 2, "ban": 1.5, "bearish": 2,
    "recall": 1.5, "recalls": 1.5, "layoffs": 1, "bubble": 1, "fears": 1, "concern": 1, "concerns": 1,
    "delay": 1, "delays": 1, "halt": 1.5, "crash": 2.5, "slump": 2, "slumps": 2,
    "fällt": 1.5, "verlust": 1.5, "sinkt": 1.5, "senkt": 1, "schwach": 1.5, "klage": 1.5, "rückgang": 1,
}
_PHRASES = {
    "price target raised": 2, "raises guidance": 2.5, "raises outlook": 2.5, "record high": 2, "all-time high": 2,
    "better than expected": 2, "strong demand": 1.5, "buy rating": 1.5, "share buyback": 1.5, "stock split": 1,
    "price target cut": -2, "price target lowered": -2, "cuts guidance": -2.5, "lowers guidance": -2.5,
    "worse than expected": -2, "job cuts": -1, "sell rating": -1.5, "52-week low": -1.5, "under pressure": -1.5,
    "price cut": -0.5, "rate cut": 0.5,
}
_NEGATORS = {"not", "no", "never", "without", "fails", "failed", "despite", "nicht", "kein", "keine"}


def sentiment(title: str) -> tuple[str, float]:
    text = title.lower()
    score = 0.0
    for phrase, w in _PHRASES.items():
        if phrase in text:
            score += w
            text = text.replace(phrase, " ")
    words = [w.strip(".,:;!?()[]\"'’‘“”") for w in text.split()]
    for i, w in enumerate(words):
        weight = _POS.get(w, 0) - _NEG.get(w, 0)
        if weight and any(p in _NEGATORS for p in words[max(0, i - 2):i]):
            weight = -0.6 * weight
        score += weight
    score = max(-1.0, min(1.0, score / 3))
    label = "Positive" if score >= 0.2 else "Negative" if score <= -0.2 else "Neutral"
    return label, round(score, 2)


# ------------------------------------------------------------------ database

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS news (ticker TEXT, title TEXT, link TEXT, published TEXT, sentiment TEXT, captured TEXT, PRIMARY KEY(ticker,link))")
        cols = {r[1] for r in con.execute("PRAGMA table_info(news)")}
        if "score" not in cols:
            con.execute("ALTER TABLE news ADD COLUMN score REAL")
        con.execute("DROP TABLE IF EXISTS evaluations")  # never populated by v1; replaced by forecasts
        con.execute("""CREATE TABLE IF NOT EXISTS forecasts (
            ticker TEXT, asof TEXT, price REAL, p_up REAL, low REAL, high REAL, horizon INTEGER, version TEXT,
            created TEXT, target_day TEXT, actual REAL, up INTEGER, in_range INTEGER, evaluated TEXT,
            PRIMARY KEY(ticker, asof))""")
        con.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")


def kv_get(key: str):
    with db() as con:
        row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def kv_set(key: str, value) -> None:
    with db() as con:
        con.execute("INSERT OR REPLACE INTO kv VALUES(?,?,?)", (key, json.dumps(value), now_iso()))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ Yahoo Finance

def http_get(url: str, timeout: int = 20, tries: int = 3) -> bytes:
    last: Exception | None = None
    for attempt in range(tries):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            with urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # network hiccups and Yahoo rate limits
            last = exc
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"{url.split('?')[0]}: {last}")


def yahoo_chart(ticker: str, period: str, interval: str) -> tuple[dict, dict]:
    """Return (series, meta). series: lists day/t/o/h/l/c/rc/v, adjusted for splits and dividends."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker, safe='-^')}"
           f"?range={period}&interval={interval}&events=div%2Csplits&includePrePost=false")
    payload = json.loads(http_get(url).decode("utf-8"))
    chart = payload.get("chart", {})
    if chart.get("error") or not chart.get("result"):
        raise RuntimeError(f"Yahoo returned no data for {ticker}.")
    result = chart["result"][0]
    meta = result.get("meta", {})
    stamps = result.get("timestamp") or []
    q = (result.get("indicators", {}).get("quote") or [{}])[0]
    adj = ((result.get("indicators", {}).get("adjclose") or [{}])[0] or {}).get("adjclose") or []

    def get(key: str, i: int):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    s = {k: [] for k in ("day", "t", "o", "h", "l", "c", "rc", "v")}
    offset = int(meta.get("gmtoffset") or 0)
    for i, stamp in enumerate(stamps):
        close = get("close", i)
        if not close:
            continue
        a = adj[i] if i < len(adj) and adj[i] else close
        f = a / close
        s["day"].append((stamp + offset) // 86400)  # exchange-local calendar day
        s["t"].append(stamp)
        s["o"].append((get("open", i) or close) * f)
        s["h"].append((get("high", i) or close) * f)
        s["l"].append((get("low", i) or close) * f)
        s["c"].append(a)
        s["rc"].append(close)
        s["v"].append(float(get("volume", i) or 0))
    return s, meta


def merge_series(full: dict, recent: dict) -> dict:
    """Overlay recent daily bars onto the cached long history (by exchange day)."""
    if not recent["day"]:
        return full
    first = recent["day"][0]
    keep = [i for i, d in enumerate(full["day"]) if d < first]
    return {k: [full[k][i] for i in keep] + recent[k] for k in full}


def fetch_news(ticker: str) -> list[dict]:
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={quote(ticker)}&region=US&lang=en-US"
    try:
        root = ET.fromstring(http_get(url, timeout=10, tries=2).decode("utf-8", "replace"))
    except Exception:
        return []
    out, seen = [], set()
    for item in root.findall(".//item"):
        title = re.sub(r"\s+", " ", (item.findtext("title") or "")).strip()
        link = (item.findtext("link") or "").strip()
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        try:
            when = parsedate_to_datetime((item.findtext("pubDate") or "").strip()).astimezone(timezone.utc).isoformat()
        except Exception:
            when = ""
        label, score = sentiment(title)
        out.append({"title": title, "link": link, "time": when, "source": "Yahoo Finance", "sentiment": label, "score": score})
    out.sort(key=lambda n: n["time"], reverse=True)
    out = out[:8]
    with db() as con:
        con.executemany("INSERT OR IGNORE INTO news(ticker,title,link,published,sentiment,captured,score) VALUES(?,?,?,?,?,?,?)",
                        [(ticker, n["title"], n["link"], n["time"], n["sentiment"], now_iso(), n["score"]) for n in out if n["link"]])
    return out


# ------------------------------------------------------------------ state

_daily: dict[str, dict] = {}          # ticker -> {"series":..., "full_at": epoch}
_status = {"phase": "starting", "message": "Starting…", "error": None, "refreshing": False, "last_attempt": None}
_snapshot: dict | None = None
_model_state: dict | None = None
_wake = threading.Event()
_train_lock = threading.Lock()


def set_status(**kw) -> None:
    _status.update(kw)


def load_daily(ticker: str) -> dict:
    entry = _daily.get(ticker)
    if entry and time.time() - entry["full_at"] < FULL_HISTORY_SECONDS:
        recent, _ = yahoo_chart(ticker, "1mo", "1d")
        entry["series"] = merge_series(entry["series"], recent)
        return entry["series"]
    series, _ = yahoo_chart(ticker, "10y", "1d")
    if len(series["c"]) < 300:
        raise RuntimeError(f"{ticker}: only {len(series['c'])} daily bars")
    _daily[ticker] = {"series": series, "full_at": time.time()}
    return series


def market_session(meta: dict) -> dict:
    period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    start, end, now = period.get("start"), period.get("end"), time.time()
    is_open = bool(start and end and start <= now < end)
    return {"open": is_open, "start": start, "end": end}


def model_key(universe: dict) -> str:
    last = max(s["day"][-1] for s in universe.values())
    return f"{model.MODEL_VERSION}:{last}"


def ensure_model(universe: dict, spy: dict, vix: dict) -> dict | None:
    """Return a trained model for today's data; retrain once per trading day."""
    global _model_state
    key = model_key(universe)
    if _model_state and _model_state.get("key") == key:
        return _model_state
    stored = kv_get("model")
    if stored and stored.get("key") == key:
        _model_state = stored
        return stored
    with _train_lock:
        set_status(phase="training", message="Training forecast model…")
        t0 = time.time()
        state = model.train(universe, spy, vix, progress=lambda m: set_status(message=m))
        state["key"] = key
        state["trained_at"] = now_iso()
        state["train_seconds"] = round(time.time() - t0, 1)
        kv_set("model", state)
        _model_state = state
        print(f"Model trained in {state['train_seconds']}s · range coverage {state['metrics']['range']['coverage']:.1%} · "
              f"direction AUC {state['metrics']['direction']['auc']:.3f} · edge={state['edge']}")
        return state


def day_str(day: int) -> str:
    return (datetime(1970, 1, 1) + timedelta(days=day)).strftime("%Y-%m-%d")


def record_forecasts(stocks: list[dict], universe: dict, session_open: bool) -> None:
    """Store today's forecast per stock and score older ones whose horizon has passed."""
    with db() as con:
        for s in stocks:
            fc, series = s.get("forecast"), universe.get(s["ticker"])
            if not fc or not series:
                continue
            asof = day_str(series["day"][-1])
            con.execute("""INSERT INTO forecasts(ticker,asof,price,p_up,low,high,horizon,version,created) VALUES(?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(ticker,asof) DO UPDATE SET price=excluded.price,p_up=excluded.p_up,low=excluded.low,
                           high=excluded.high,created=excluded.created WHERE forecasts.evaluated IS NULL""",
                        (s["ticker"], asof, s["price"], fc["p_up"], fc["low"], fc["high"], fc["horizon"], model.MODEL_VERSION, now_iso()))
        pending = con.execute("SELECT * FROM forecasts WHERE evaluated IS NULL").fetchall()
        for row in pending:
            series = universe.get(row["ticker"])
            if not series:
                continue
            days = [day_str(d) for d in series["day"]]
            if row["asof"] not in days:
                continue
            target = days.index(row["asof"]) + row["horizon"]
            complete = len(days) - (1 if session_open else 0)
            if target >= complete:
                continue
            actual = series["rc"][target]
            if not (0.5 < actual / row["price"] < 2):  # split or bad data: skip scoring
                con.execute("UPDATE forecasts SET evaluated=?, target_day=? WHERE ticker=? AND asof=?", (now_iso(), days[target], row["ticker"], row["asof"]))
                continue
            con.execute("UPDATE forecasts SET target_day=?, actual=?, up=?, in_range=?, evaluated=? WHERE ticker=? AND asof=?",
                        (days[target], actual, int(actual > row["price"]), int(row["low"] <= actual <= row["high"]), now_iso(), row["ticker"], row["asof"]))


def track_record() -> dict:
    with db() as con:
        rows = con.execute("SELECT * FROM forecasts WHERE actual IS NOT NULL ORDER BY asof DESC").fetchall()
        open_count = con.execute("SELECT COUNT(*) FROM forecasts WHERE evaluated IS NULL").fetchone()[0]

    def summarise(items):
        n = len(items)
        if not n:
            return {"n": 0}
        brier = sum((r["p_up"] - r["up"]) ** 2 for r in items) / n
        return {"n": n, "range_hits": sum(r["in_range"] for r in items), "range_rate": round(sum(r["in_range"] for r in items) / n, 3),
                "up_rate": round(sum(r["up"] for r in items) / n, 3), "brier": round(brier, 4)}

    by = {}
    for r in rows:
        by.setdefault(r["ticker"], []).append(r)
    return {"overall": summarise(rows), "pending": open_count,
            "by_ticker": {t: {**summarise(v), "recent": [{"asof": r["asof"], "target": r["target_day"], "price": r["price"], "actual": r["actual"],
                                                          "low": r["low"], "high": r["high"], "in_range": bool(r["in_range"]), "p_up": r["p_up"]} for r in v[:5]]}
                          for t, v in by.items()}}


def chart_series(daily: dict, intraday: dict | None) -> dict:
    pts = lambda s, idx: [[s["t"][i], float(f"{s['c'][i]:.5g}")] for i in idx]
    n = len(daily["c"])
    out = {"1M": pts(daily, range(max(0, n - 22), n)), "1J": pts(daily, range(max(0, n - 252), n))}
    out["MAX"] = pts(daily, sorted(set(range(0, n, 10)) | {n - 1}))
    if intraday and intraday["c"]:
        last_day = intraday["day"][-1]
        out["1T"] = pts(intraday, [i for i, d in enumerate(intraday["day"]) if d == last_day])
        out["1W"] = pts(intraday, sorted(set(range(0, len(intraday["c"]), 2)) | {len(intraday["c"]) - 1}))
    else:
        out["1T"], out["1W"] = [], pts(daily, range(max(0, n - 5), n))
    return out


def build_stock(ticker: str, daily: dict, intraday: tuple[dict, dict] | None, news: list[dict], state: dict | None,
                spy: dict, vix: dict) -> dict:
    meta_info = SYMBOLS[ticker]
    iseries, imeta = intraday if intraday else (None, {})
    price = float(imeta.get("regularMarketPrice") or daily["rc"][-1])
    prev = imeta.get("previousClose")
    if not prev:
        prev = daily["rc"][-2] if len(daily["rc"]) > 1 else price
    change = (price / prev - 1) * 100 if prev else 0.0
    fc = model.forecast(state, ticker, daily, spy, vix, price) if state else None
    rated = [n["score"] for n in news[:8]]
    year = daily["rc"][-252:]
    return {
        "ticker": ticker, **meta_info, "price": round(price, 5 if price < 20 else 2), "prevClose": round(float(prev), 5 if price < 20 else 2), "change": round(change, 3), "dayHigh": imeta.get("regularMarketDayHigh"), "dayLow": imeta.get("regularMarketDayLow"),
        "yearHigh": float(f"{max(year):.6g}"), "yearLow": float(f"{min(year):.6g}"), "marketTime": imeta.get("regularMarketTime"),
        "forecast": fc, "news": news[:5], "newsTone": round(sum(rated) / len(rated), 2) if rated else None,
        "chart": chart_series(daily, iseries),
    }


def refresh() -> None:
    global _snapshot
    set_status(refreshing=True, last_attempt=now_iso(), message="Loading Yahoo Finance data…")
    tickers = list(SYMBOLS) + MARKET
    universe, errors = {}, {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        daily_jobs = {t: pool.submit(load_daily, t) for t in tickers}
        intra_jobs = {t: pool.submit(yahoo_chart, t, "5d", "15m") for t in SYMBOLS}
        news_jobs = {t: pool.submit(fetch_news, t) for t in SYMBOLS}
        for t, job in daily_jobs.items():
            try:
                universe[t] = job.result()
            except Exception as exc:
                errors[t] = str(exc)
        intraday = {}
        for t, job in intra_jobs.items():
            try:
                intraday[t] = job.result()
            except Exception as exc:
                errors.setdefault(t, f"intraday: {exc}")
        news = {t: (job.result() if not job.exception() else []) for t, job in news_jobs.items()}
    for t, e in errors.items():
        print(f"{t}: {e}")
    stocks_data = {t: universe[t] for t in SYMBOLS if t in universe}
    if not stocks_data:
        raise RuntimeError("Yahoo Finance is unavailable or returned no price data.")
    spy = dict(zip(universe["SPY"]["day"], universe["SPY"]["c"])) if "SPY" in universe else {}
    vix = dict(zip(universe["^VIX"]["day"], universe["^VIX"]["c"])) if "^VIX" in universe else {}
    session = market_session(next(iter(intraday.values()))[1]) if intraday else {"open": False}

    def publish(state):
        global _snapshot
        stocks = []
        for t in SYMBOLS:
            if t not in stocks_data:
                continue
            try:
                stocks.append(build_stock(t, stocks_data[t], intraday.get(t), news.get(t, []), state, spy, vix))
            except Exception as exc:
                print(f"{t}: build failed: {exc}")
        if state:
            record_forecasts(stocks, stocks_data, session["open"])
        metrics = state["metrics"] if state else None
        _snapshot = {
            "mode": "live", "provider": "Yahoo Finance", "updated_at": now_iso(), "market": session,
            "stocks": stocks, "missing": sorted(set(SYMBOLS) - set(stocks_data)),
            "model": {"ready": bool(state), "version": model.MODEL_VERSION, "horizon": model.HORIZON,
                      "trained_at": state.get("trained_at") if state else None, "samples": state.get("trained_on") if state else None,
                      "metrics": {k: v for k, v in metrics.items() if k != "per_stock"} if metrics else None,
                      "features": model.FEATURE_NAMES},
            "track": track_record(),
            "disclaimer": "Quotes may be delayed. Experimental model; not investment advice.",
        }
        kv_set("snapshot", _snapshot)

    state = _model_state if _model_state and spy and vix else None
    if spy and vix:
        stored = _model_state or kv_get("model")
        if stored and stored.get("version") != model.MODEL_VERSION:
            stored = None
        if stored and stored.get("key") != model_key(stocks_data):
            publish(stored)  # show fresh prices with yesterday's model while retraining
        try:
            state = ensure_model(stocks_data, spy, vix)
        except Exception as exc:
            traceback.print_exc()
            state = stored
            set_status(error=f"Model training failed: {exc}")
    publish(state)
    set_status(phase="ready", message="Up to date", error=None, refreshing=False)


def refresh_loop() -> None:
    while True:
        try:
            refresh()
        except Exception as exc:
            traceback.print_exc()
            set_status(phase="error" if not _snapshot else "ready", error=str(exc), refreshing=False,
                       message="Data refresh failed; showing last saved data" if _snapshot else "Data refresh failed")
        _wake.wait(REFRESH_SECONDS)
        _wake.clear()


# ------------------------------------------------------------------ HTTP

class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml", ".js": "text/javascript"}
    _last_force = 0.0

    def log_message(self, fmt, *args):
        first, second = (str(args[0]) if args else ""), (str(args[1]) if len(args) > 1 else "")
        if "/api/" in first or second.startswith(("4", "5")) or first.startswith(("4", "5")):
            sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

    def send_json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/overview":
            if parse_qs(url.query).get("force") and time.time() - Handler._last_force > 30 and not _status["refreshing"]:
                Handler._last_force = time.time()
                _wake.set()
            status = {k: _status[k] for k in ("phase", "message", "error", "refreshing", "last_attempt")}
            if _snapshot:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(_snapshot["updated_at"])).total_seconds()
                return self.send_json({**_snapshot, "status": status, "age_seconds": round(age), "stale": age > REFRESH_SECONDS * 3})
            return self.send_json({"mode": "loading" if _status["phase"] != "error" else "unavailable", "status": status}, 503)
        if url.path == "/api/model":
            st = _model_state or kv_get("model")
            if not st:
                return self.send_json({"ready": False}, 503)
            return self.send_json({k: st[k] for k in ("version", "trained_at", "trained_on", "metrics", "alpha", "edge", "train_seconds") if k in st})
        if url.path == "/api/health":
            return self.send_json({"ok": True, "status": _status})
        if url.path.endswith((".py", ".sqlite3", ".bat")) or "/." in url.path or url.path.startswith(("/__pycache__", "/.venv")):
            return self.send_error(404)
        return super().do_GET()


def main() -> None:
    global _snapshot, _model_state
    init_db()
    _snapshot = kv_get("snapshot")  # last saved data: the page is usable instantly after a restart
    _model_state = kv_get("model")
    if _model_state and _model_state.get("version") != model.MODEL_VERSION:
        _model_state = None
    if _snapshot and (_snapshot.get("model") or {}).get("version") != model.MODEL_VERSION:
        _snapshot = None
    threading.Thread(target=refresh_loop, daemon=True, name="refresh").start()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://localhost:{port}"
    print(f"MarktRadar is running at {url}" + (f" (listening on {host})" if host != "127.0.0.1" else ""))
    print("Data refreshes every 5 minutes in the background. Press Ctrl+C to stop.")
    if "--open" in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


STATIC_FILES = ["index.html", "sw.js", "manifest.webmanifest", "icon.svg"]
STATIC_REFRESH_SECONDS = 600  # GitHub Actions schedule during market hours


def build_static(out_dir: Path, state_dir: Path) -> None:
    """One refresh without a web server, written as a static site (GitHub Pages).

    State (trained model + saved forecasts) lives as small JSON files in `state_dir`
    so the track record survives between runs.
    """
    global DB, _snapshot, _model_state
    import shutil
    state_dir.mkdir(parents=True, exist_ok=True)
    DB = state_dir / "_build.sqlite3"
    DB.unlink(missing_ok=True)
    init_db()
    model_file, forecast_file = state_dir / "model.json", state_dir / "forecasts.json"
    if model_file.exists():
        stored = json.loads(model_file.read_text(encoding="utf-8"))
        if stored.get("version") == model.MODEL_VERSION:
            kv_set("model", stored)
            _model_state = stored
    if forecast_file.exists():
        rows = json.loads(forecast_file.read_text(encoding="utf-8"))
        with db() as con:
            for r in rows:
                con.execute(f"INSERT OR REPLACE INTO forecasts({','.join(r)}) VALUES({','.join('?' * len(r))})", list(r.values()))
    refresh()
    if not _snapshot:
        raise RuntimeError("No data could be loaded.")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "api").mkdir(exist_ok=True)
    for name in STATIC_FILES:
        shutil.copy2(ROOT / name, out_dir / name)
    snap = {**_snapshot, "static": True, "refresh_seconds": STATIC_REFRESH_SECONDS,
            "status": {"phase": "ready", "message": "Up to date", "error": None, "refreshing": False, "last_attempt": now_iso()}}
    (out_dir / "api" / "overview").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    st = _model_state or {}
    (out_dir / "api" / "model").write_text(json.dumps({k: st[k] for k in ("version", "trained_at", "trained_on", "metrics", "alpha", "edge", "train_seconds") if k in st}), encoding="utf-8")
    (out_dir / ".nojekyll").write_text("")
    if _model_state:
        model_file.write_text(json.dumps(_model_state), encoding="utf-8")
    with db() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM forecasts ORDER BY asof, ticker")]
    forecast_file.write_text(json.dumps(rows, indent=0), encoding="utf-8")
    try:
        DB.unlink(missing_ok=True)
    except OSError:
        pass
    print(f"Static site written to {out_dir} · {len(snap['stocks'])} stocks · {len(rows)} saved forecasts")


if __name__ == "__main__":
    if "--build" in sys.argv:
        args = sys.argv[1:]
        out = Path(args[args.index("--build") + 1]).resolve()
        state = Path(args[args.index("--state") + 1]).resolve() if "--state" in args else out.parent / "state"
        build_static(out, state)
    else:
        main()
