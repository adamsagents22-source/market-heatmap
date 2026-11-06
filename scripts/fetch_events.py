"""Builds site/events.json: earnings calendar, IPO calendar and market headlines.

Sources: Nasdaq's public earnings + IPO calendar APIs and CNBC's public RSS feeds.
Each section is fetched independently. If a section fails, the previous
version of that section is kept from the live site so a hiccup never blanks a tab.
"""
import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import requests

OUT = os.path.join(os.path.dirname(__file__), "..", "site", "events.json")
PAGES_URL = os.environ.get("PAGES_URL", "").rstrip("/") + "/"
ET_TZ = ZoneInfo("America/New_York")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}

# Order matters: a story that appears in several feeds keeps the first (most specific) tag.
# "General" is CNBC's catch-all top news (includes politics); the site's default "Business" view hides it.
NEWS_FEEDS = [
    ("Earnings", "https://www.cnbc.com/id/15839135/device/rss/rss.html"),
    ("Investing", "https://www.cnbc.com/id/15839069/device/rss/rss.html"),
    ("Finance", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
    ("Economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("Tech", "https://www.cnbc.com/id/19854910/device/rss/rss.html"),
    ("Business", "https://www.cnbc.com/id/10001147/device/rss/rss.html"),
    ("General", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
]

MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun",
                                           "jul", "aug", "sep", "oct", "nov", "dec"])}
DETAIL_MIN_CAP = 2e9      # only fetch revenue/net income/history for companies above this market cap
DETAIL_PER_DAY = 30       # ...and at most this many per day (largest first)
DETAIL_BUDGET_S = 240     # stop fetching details after this many seconds


def num(s):
    if s is None:
        return None
    s = str(s).replace("$", "").replace(",", "").replace("%", "").replace("(", "-").replace(")", "").strip()
    if s in ("", "NA", "N/A", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def get(url, as_json=True, tries=3):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.json() if as_json else r.content
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def business_days(start, before, after):
    days, d = [], start - timedelta(days=before)
    while d <= start + timedelta(days=after):
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def quarter_key(label):
    """'Sep/2026' -> (2026, 9)"""
    try:
        m, y = label.split("/")
        return int(y), MONTHS[m[:3].lower()]
    except Exception:  # noqa: BLE001
        return None


def fetch_financials(sym, qlabel):
    """Latest-quarter and prior-quarter revenue / net income in dollars, or None.
    Only returns data when the newest quarter on file is the quarter that was just reported."""
    j = get(f"https://api.nasdaq.com/api/company/{sym}/financials?frequency=2", tries=2)
    t = ((j.get("data") or {}).get("incomeStatementTable")) or {}
    hdr, rows = t.get("headers") or {}, t.get("rows") or []
    try:
        mo, _, yr = str(hdr.get("value2", "")).split("/")
        newest = (int(yr), int(mo))
    except Exception:  # noqa: BLE001
        return {}
    want = quarter_key(qlabel)
    if want and newest != want:
        return {}  # the filing for this quarter is not in yet

    def pair(label):
        for r in rows:
            if (r.get("value1") or "").strip() == label:
                a, b = num(r.get("value2")), num(r.get("value3"))
                return None if a is None else [round(a * 1000), None if b is None else round(b * 1000)]
        return None

    out = {}
    rev, ni = pair("Total Revenue"), pair("Net Income")
    if rev:
        out["rev"] = rev
    if ni:
        out["ni"] = ni
    return out


def fetch_history(sym):
    j = get(f"https://api.nasdaq.com/api/company/{sym}/earnings-surprise", tries=2)
    rows = (((j.get("data") or {}).get("earningsSurpriseTable")) or {}).get("rows") or []
    return [[r.get("fiscalQtrEnd") or "", r.get("dateReported") or "", num(r.get("eps")),
             num(r.get("consensusForecast")), num(r.get("percentageSurprise"))] for r in rows[:4]]


def enrich(days, old):
    """Attach revenue / net income / EPS history to reported companies. Re-uses what the
    previous run already fetched so each update only does a handful of new requests."""
    cache = {}
    for d in (old.get("earnings") or []):
        for r in d.get("rows", []):
            if r.get("hist") is not None:
                cache[(d["date"], r["t"])] = r
    today = datetime.now(ET_TZ).date()
    todo = []
    for day in days:
        age = (today - datetime.strptime(day["date"], "%Y-%m-%d").date()).days
        reported = [r for r in day["rows"] if r["eps"] is not None and r["c"] >= DETAIL_MIN_CAP][:DETAIL_PER_DAY]
        for r in reported:
            c = cache.get((day["date"], r["t"]))
            if c and ("rev" in c or age > 5):   # recent ones without revenue yet are retried
                for k in ("rev", "ni", "hist"):
                    if k in c:
                        r[k] = c[k]
            else:
                todo.append(r)
                if c and "hist" in c:
                    r["hist"] = c["hist"]
    if not todo:
        return
    # find the quarter label for each row
    deadline = time.time() + DETAIL_BUDGET_S

    def work(r):
        if time.time() > deadline:
            return
        try:
            r.update(fetch_financials(r["t"], r["q"]))
        except Exception as e:  # noqa: BLE001
            print(f"financials {r['t']}: {e}", file=sys.stderr)
        if "hist" not in r and time.time() < deadline:
            try:
                r["hist"] = fetch_history(r["t"])
            except Exception as e:  # noqa: BLE001
                print(f"history {r['t']}: {e}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=5) as ex:
        list(ex.map(work, todo))
    print(f"details fetched for {len(todo)} companies", file=sys.stderr)


def fetch_earnings(old=None):
    today = datetime.now(ET_TZ).date()
    out = []
    for d in business_days(today, 10, 7):
        data = get(f"https://api.nasdaq.com/api/calendar/earnings?date={d.isoformat()}").get("data") or {}
        rows = data.get("rows") or []
        items = []
        for r in rows:
            sym = (r.get("symbol") or "").strip().upper()
            if not sym:
                continue
            t = r.get("time") or ""
            items.append({
                "t": sym,
                "n": (r.get("name") or "").strip(),
                "when": "pre" if "pre" in t else "after" if "after" in t else "",
                "eps": num(r.get("eps")),
                "est": num(r.get("epsForecast")),
                "sur": num(r.get("surprise")),
                "c": num(r.get("marketCap")) or 0,
                "q": r.get("fiscalQuarterEnding") or "",
            })
        items.sort(key=lambda x: -x["c"])
        out.append({"date": d.isoformat(), "total": len(items), "rows": items[:80]})
        time.sleep(0.4)
    if not any(day["rows"] for day in out):
        raise RuntimeError("no earnings rows at all")
    try:
        enrich(out, old or {})
    except Exception as e:  # noqa: BLE001
        print(f"enrich failed (continuing without details): {e}", file=sys.stderr)
    return out


def fetch_ipos():
    today = datetime.now(ET_TZ).date()
    months = [today.strftime("%Y-%m"), (today.replace(day=1) + timedelta(days=32)).strftime("%Y-%m")]
    prev = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    upcoming, priced, filed = [], [], []
    for m in [prev] + months:
        data = get(f"https://api.nasdaq.com/api/ipo/calendar?date={m}").get("data") or {}
        for r in ((data.get("upcoming") or {}).get("upcomingTable") or {}).get("rows") or []:
            upcoming.append({
                "t": r.get("proposedTickerSymbol") or "", "n": r.get("companyName") or "",
                "ex": r.get("proposedExchange") or "", "price": r.get("proposedSharePrice") or "",
                "shares": r.get("sharesOffered") or "", "date": r.get("expectedPriceDate") or "",
                "amt": r.get("dollarValueOfSharesOffered") or "",
            })
        for r in (data.get("priced") or {}).get("rows") or []:
            priced.append({
                "t": r.get("proposedTickerSymbol") or "", "n": r.get("companyName") or "",
                "ex": r.get("proposedExchange") or "", "price": r.get("proposedSharePrice") or "",
                "shares": r.get("sharesOffered") or "", "date": r.get("pricedDate") or "",
                "amt": r.get("dollarValueOfSharesOffered") or "",
            })
        for r in (data.get("filed") or {}).get("rows") or []:
            filed.append({
                "t": r.get("proposedTickerSymbol") or "", "n": r.get("companyName") or "",
                "date": r.get("filedDate") or "", "amt": r.get("dollarValueOfSharesOffered") or "",
            })
        time.sleep(0.4)

    def key(x):
        try:
            return datetime.strptime(x["date"], "%m/%d/%Y")
        except ValueError:
            return datetime.min

    # de-dupe by company, newest first for priced/filed, soonest first for upcoming
    def dedupe(rows):
        seen, res = set(), []
        for x in rows:
            k = (x["n"], x["t"])
            if k not in seen:
                seen.add(k)
                res.append(x)
        return res

    return {
        "upcoming": sorted(dedupe(upcoming), key=key),
        "priced": sorted(dedupe(priced), key=key, reverse=True)[:40],
        "filed": sorted(dedupe(filed), key=key, reverse=True)[:25],
    }


def fetch_news():
    items, seen = [], set()
    ok = 0
    for tag, url in NEWS_FEEDS:
        try:
            root = ET.fromstring(get(url, as_json=False))
        except Exception as e:  # noqa: BLE001
            print(f"feed {tag} failed: {e}", file=sys.stderr)
            continue
        ok += 1
        for it in root.iter("item"):
            link = (it.findtext("link") or "").strip()
            title = (it.findtext("title") or "").strip()
            if not link or not title or link in seen:
                continue
            seen.add(link)
            try:
                ts = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc)
            except Exception:  # noqa: BLE001
                continue
            items.append({"title": title, "url": link, "src": "CNBC", "tag": tag,
                          "ts": ts.isoformat(timespec="seconds")})
    if not ok or not items:
        raise RuntimeError("no news")
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:120]


def previous():
    try:
        return requests.get(PAGES_URL + "events.json", timeout=20).json()
    except Exception:  # noqa: BLE001
        return {}


def main():
    old = previous()
    payload = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    failed = []
    for name, fn in (("earnings", lambda: fetch_earnings(old)), ("ipos", fetch_ipos), ("news", fetch_news)):
        try:
            payload[name] = fn()
            print(f"{name}: ok", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"{name} failed: {e}", file=sys.stderr)
            failed.append(name)
            if name in old:
                payload[name] = old[name]
    if all(n not in payload for n in ("earnings", "ipos", "news")):
        sys.exit("events: nothing to publish")
    payload["stale"] = failed
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"wrote events.json (failed sections: {failed or 'none'})")


if __name__ == "__main__":
    main()
