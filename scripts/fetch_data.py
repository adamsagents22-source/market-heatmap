"""Builds site/data.json for the heatmap.

Primary source: Nasdaq's public stock screener (every US-listed stock, with
market cap, % change, sector, industry) in one request.
Fallback: if Nasdaq refuses, take the universe from the currently deployed
data.json and refresh % changes with yfinance.
If both fail, the script exits non-zero and the last good deploy stays live.
"""
import json, os, re, sys, time
from datetime import datetime, timezone

import requests

OUT = os.path.join(os.path.dirname(__file__), "..", "site", "data.json")
MAX_STOCKS = int(os.environ.get("MAX_STOCKS", "2500"))
PAGES_URL = os.environ.get("PAGES_URL", "").rstrip("/") + "/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}

NAME_CUT = re.compile(
    r"\s+(Class [A-Z]\b.*|Common Stock.*|Ordinary Shares.*|American Depositary.*|"
    r"American Depository.*|Depositary Shares.*|ADS\b.*|Units?\b.*|Common Shares.*|"
    r"Subordinate Voting.*|Capital Stock.*)$",
    re.I,
)
SKIP_NAME = re.compile(r"warrant|preferred|right[s]?\b|\bunit[s]?\b|notes? due|debenture|% ", re.I)


def num(s):
    if s is None:
        return None
    s = str(s).replace("$", "").replace(",", "").replace("%", "").strip()
    if s in ("", "NA", "N/A", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def clean_name(n):
    n = NAME_CUT.sub("", (n or "").strip())
    n = re.sub(r"\s+", " ", n).strip(" ,.-")
    return n


def from_nasdaq():
    url = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true"
    last = None
    for attempt in range(4):
        try:
            r = requests.get(url, headers=HEADERS, timeout=45)
            r.raise_for_status()
            rows = r.json()["data"]["rows"]
            if len(rows) < 1000:
                raise ValueError(f"only {len(rows)} rows")
            return rows
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"nasdaq attempt {attempt + 1} failed: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Nasdaq screener unavailable: {last}")


def build_from_rows(rows):
    stocks, seen_names = [], set()
    for r in rows:
        sym = (r.get("symbol") or "").strip().upper()
        raw_name = r.get("name") or ""
        if not sym or "^" in sym or SKIP_NAME.search(raw_name):
            continue
        sym = sym.replace("/", ".")
        mcap = num(r.get("marketCap"))
        pct = num(r.get("pctchange"))
        if not mcap or mcap <= 0:
            continue
        stocks.append({
            "t": sym,
            "n": clean_name(raw_name) or sym,
            "s": (r.get("sector") or "").strip() or "Other",
            "i": (r.get("industry") or "").strip() or "Other",
            "c": round(mcap),
            "p": None if pct is None else round(pct, 2),
            "px": num(r.get("lastsale")),
        })
    stocks.sort(key=lambda d: -d["c"])
    # keep one share class per company (e.g. GOOGL over GOOG)
    out = []
    for s in stocks:
        key = s["n"].lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        out.append(s)
        if len(out) >= MAX_STOCKS:
            break
    return out


def from_yfinance_fallback():
    import yfinance as yf

    prev = requests.get(PAGES_URL + "data.json", timeout=30).json()["stocks"]
    tickers = [s["t"].replace(".", "-") for s in prev]
    df = yf.download(tickers, period="5d", interval="1d", group_by="ticker",
                     threads=True, progress=False, auto_adjust=False)
    for s in prev:
        yt = s["t"].replace(".", "-")
        try:
            closes = df[yt]["Close"].dropna()
            if len(closes) >= 2:
                last, prior = float(closes.iloc[-1]), float(closes.iloc[-2])
                if s.get("px"):
                    s["c"] = round(s["c"] * last / s["px"])
                s["px"] = round(last, 2)
                s["p"] = round((last / prior - 1) * 100, 2)
        except Exception:  # noqa: BLE001
            pass
    return prev


def main():
    source = "Nasdaq screener"
    try:
        stocks = build_from_rows(from_nasdaq())
    except Exception as e:  # noqa: BLE001
        print(f"{e}; trying yfinance fallback", file=sys.stderr)
        stocks = from_yfinance_fallback()
        source = "Yahoo Finance"
    if len(stocks) < 100:
        sys.exit(f"Refusing to publish: only {len(stocks)} stocks")
    payload = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "count": len(stocks),
        "stocks": stocks,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"wrote {len(stocks)} stocks from {source}")


if __name__ == "__main__":
    main()
