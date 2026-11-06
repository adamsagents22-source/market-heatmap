# Market Heatmap

A zoomable, self-updating US stock market heatmap. Covers the top ~2,500 US-listed stocks by market cap. Stocks are grouped by sector and industry, and tile size is set by market cap. Scroll or pinch to zoom until even the smallest companies are readable. You can also search a ticker to jump to it.

Live site: https://adamsagents22-source.github.io/market-heatmap/

## How it works

```
.github/workflows/update.yml   ← runs every 10 min, Mon–Fri, 13:00–21:59 UTC
scripts/fetch_data.py          ← pulls data → site/data.json
site/index.html                ← the heatmap (reads data.json)
```

- **Data:** Nasdaq's public stock screener (~15 min delayed), with Yahoo Finance as a fallback. No API keys.
- **If a run fails,** the last good version of the site stays up.
- **Manual refresh:** Actions → Update heatmap → Run workflow.
- **Auto-pause:** GitHub pauses scheduled workflows after 60 days with no commits. Re-enable it in the Actions tab if updates stop.
- **Number of stocks:** `MAX_STOCKS` in `update.yml`.
