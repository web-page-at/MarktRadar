# MarktRadar

Responsive stock overview for ten large US companies: **NVDA, MSFT, AAPL, AMZN, GOOGL, META, TSLA, AVGO, BRK-B and JPM**. The Python server loads Yahoo Finance prices and RSS headlines in the background, trains a forecast model once per trading day and keeps a live track record of its own forecasts. Only the Python standard library is used.

## Online (phone and PC)

**https://web-page-at.github.io/MarktRadar/** – runs on GitHub, no PC needed. A GitHub Actions job (`.github/workflows/pages.yml`) loads the data every 15 minutes while the US market is open (hourly otherwise), retrains the model once per trading day and publishes the result to GitHub Pages. The trained model and the saved forecasts are kept as JSON on the `data` branch. On the iPhone: open the link in Safari → Share → **Add to Home Screen**.

Note: GitHub pauses scheduled jobs in public repositories after 60 days without activity; re-enable it under *Actions* if that happens.

## Start on Windows

1. Install Python 3.12 or later.
2. Double-click `Start_MarktRadar.bat`. It creates a virtual environment if needed, starts the server and opens the browser.

Or in PowerShell in this folder:

```powershell
.venv\Scripts\python.exe server.py --open
```

The page opens at `http://localhost:8000`. On the very first start the server loads ten years of prices and trains the model (about 30–60 seconds); the page shows the progress. After that, data refreshes every five minutes in the background and the last data is saved, so a restart shows prices immediately.

## What the model does (v2)

**Likely range (the part that works).** Volatility clusters, so how far a stock may move is predictable. A HAR volatility model forecasts the next 5-day volatility from 1/5/22-day high-low (Parkinson) volatility, 60-day volatility, an EWMA and the VIX. Per-stock quantiles of past scaled returns turn that into an 80% price range.

**Direction (honestly reported).** A pooled, regularised logistic regression uses 22 features (momentum over 1–252 days, strength vs. the S&P 500, RSI, distance to 50/200-day averages, drawdown, volume, gaps, VIX level/change, market trend). Its probability is blended with each stock's historical up-rate; the blend weight is chosen out of sample. If the model has no measurable edge, the app shows the base rate and says “No reliable edge” instead of a confident-looking guess.

**Validation.** Everything is tested walk-forward: train only on the past, test the next half year, repeat (12 folds, 5-day purge gap). Results on 10 years of data (Nov 2026):

| | v1 (old) | v2 (new) |
|---|---|---|
| Direction, held-out | balanced accuracy 33–36% (3 classes, chance = 33%) | AUC 0.48, accuracy 55.0% vs. 55.1% for “always up” → no edge, so base rate is used |
| 5-day range | ±1.5×vol, coverage not stated (measured 84%, width 13.5%) | 80% target, measured **80.0%**, width **11.8%** (−13%), per stock 77–85% |
| Training time | ~10 s per stock on every refresh | ~20 s for all stocks once per day, in the background |

**Live track record.** Every day's forecast is saved in `marktradar.sqlite3` (table `forecasts`) and scored after five trading days: did the price land in the range, and how good was the up-probability (Brier score)? The “How accurate is the model?” panel shows the result.

**News.** Yahoo Finance RSS headlines get a weighted keyword/phrase sentiment with simple negation handling. It is shown as context only and is not used by the model (no historical news archive is available to validate it). Headlines are stored for later evaluation.

## API

- `GET /api/overview` – prices, charts, forecasts, news, model metrics and track record (`?force=1` triggers a refresh)
- `GET /api/model` – full walk-forward metrics including per-stock results
- `GET /api/health` – refresh status

## iPhone and notifications

The page is installable as a PWA. For the iPhone the server must be reachable on your Wi-Fi: start it with `$env:HOST="0.0.0.0"; .venv\Scripts\python.exe server.py` and open `http://<PC-IP>:8000`. iOS notifications need HTTPS and a home-screen installation. While the page is open and notifications are allowed, it alerts on daily moves of ±3% and strongly positive/negative new headlines.

## Data and model limits

Prices may be delayed, incomplete or temporarily unavailable. This is an experimental learning project; backtest results do not guarantee future accuracy and nothing here is investment advice.

Prices come from Yahoo Finance's chart endpoint and headlines from Yahoo Finance RSS. Before public or commercial redistribution, review Yahoo's terms and the display and redistribution rights for the market data and news.
