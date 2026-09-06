# Mesh

A multi-market stock research and analysis terminal with an opinionated, data-driven
view on securities. It ingests prices, news, social chatter, fundamentals and
institutional filings, scores every security through a quantitative ensemble, and
renders a single, defensible **verdict** — `BULL`, `BEAR` or `NEUTRAL` — in a fast,
keyboard-first terminal UI.

> Mesh is a research and education tool. All trading is **paper trading only** —
> no real orders are ever placed and no brokerage integration exists.

---

## Features

### Verdict engine

Every security is reduced to a combined score in `[-1, +1]` from multiple independent
signals, then mapped to a verdict via configurable thresholds.

| Signal | Description |
| --- | --- |
| LSTM price forecast | Per-symbol PyTorch LSTM predicting next-day return direction and confidence |
| Sentiment LSTM | News-headline sentiment model over market RSS/financial feeds |
| Technical / momentum | Trend and momentum factors over price history |
| News | Scored RSS + financial news with source weighting |
| Social | Reddit discussion sentiment (requires API credentials) |
| Fundamentals | Valuation and ratio checks where data is available |
| Institutional | SEC EDGAR 13F hedge-fund holdings and quarterly changes |

Signals are combined through an **Investment Committee** model (quant, social and
regime weights) and a **quant ensemble** (LSTM, GBM and momentum). Missing signals are
renormalized rather than fabricated — a capability is never treated as a bullish or
bearish input.

### Terminal interface

- **Overview** — market-wide dashboard, indices and watchlist summary
- **Scanner** — ranked universe scan with verdict, score and capability status
- **Portfolio** — personal watchlist with quick verdicts
- **Alerts** — persistent above/below price targets with distance-to-trigger,
  pause/re-arm controls and automatic terminal notifications
- **Paper** — simulated portfolio with orders, equity curve, risk, stats and leaderboard
- **Screener** — filter the universe by fundamental / technical criteria
- **LSTM** — run and inspect per-symbol price forecasts and model training
- **Sim / Backtest** — historical replay and strategy backtesting
- **Indexes** — global index tape and history
- **Hedge Funds** — SEC 13F fund summaries, holdings and quarterly changes

Plus a **security Dossier** drawer with deep links (`#/dossier/MARKET:TICKER`),
global search, a notifications bell, a live ticker tape, and dark / light / system
themes.

---

## Tech stack

| Layer | Technology |
| --- | --- |
| Backend | Python 3.14, FastAPI, Uvicorn |
| ML | PyTorch (LSTM), NumPy (gradient-boosted / momentum ensembles) |
| Data | SQLite, yfinance, httpx, feedparser, PRAW (Reddit), SEC EDGAR |
| Frontend | React 18, Vite 5, Chart.js |
| Tooling | uv (Python), npm (frontend), pytest, node:test |

---

## Project structure

```
.
├── data/                       # SQLite databases (gitignored)
├── frontend/
│   ├── src/                    # React application
│   │   ├── components/         # Tabs, Drawer, charts, auth, UI primitives
│   │   ├── api.js              # Backend API client
│   │   ├── nav.js              # Canonical security routing (#/dossier/...)
│   │   └── App.jsx             # Application shell, context, tabs
│   ├── test/                   # node:test unit tests
│   └── vite.config.js          # Vite config + /api proxy to :8899
├── src/stock_alert_app/
│   ├── web_app.py              # FastAPI app + all routes
│   ├── verdict.py              # Multi-signal verdict engine
│   ├── analysis.py             # Canonical analysis builder
│   ├── dossier.py              # Bull/bear factors + committee decision
│   ├── paper.py                # Paper trading simulation
│   ├── simulation.py           # Backtesting / replay
│   ├── models/                 # LSTM models + per-symbol checkpoints
│   ├── markets/                # Market definitions (JSON, one per market)
│   └── ...                     # refresh, screener, notifications, auth, etc.
├── tests/                      # Python (pytest) test suite
└── pyproject.toml              # uv project definition
```

---

## Getting started

### Prerequisites

- Python **3.14** (managed with [uv](https://docs.astral.sh/uv/))
- Node.js 18+ and npm

### 1. Backend

```bash
uv sync
uv run stock-alert-app serve --host 127.0.0.1 --port 8899
```

The backend listens on `http://127.0.0.1:8899` by default in this workflow. The
frontend dev proxy expects this port.

### 2. Frontend (development)

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`. Vite proxies `/api` requests to the backend on `8899`.

### 3. Production build

```bash
cd frontend
npm run build
```

The backend serves the built app from `frontend/dist/`. Once built, the whole
application is available directly from the backend:

```bash
uv run stock-alert-app serve --host 127.0.0.1 --port 8899
# open http://127.0.0.1:8899
```

---

## Configuration

Configuration is read from environment variables (optionally via a `.env` file).
Sensible defaults are provided for everything.

| Variable | Default | Description |
| --- | --- | --- |
| `STOCK_ALERT_DB` | `data/stock_verdict.db` | SQLite database path |
| `STOCK_ALERT_DATA` | `data` | Data directory |
| `LLM_API_KEY` / `LLM_MODEL` | — / `gpt-4o-mini` | LLM used by the research agent |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | — / `gemini-3.5-flash-lite` | Gemini fallback for the agent |
| `NEWS_API_KEY` | — | Optional news API key |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | — | Enables the social/Reddit signal |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | `http://localhost:11434` / `gemma4:latest` | Local LLM option |
| `STOCK_ALERT_LSTM_WEIGHT` | `0.60` | LSTM signal weight |
| `STOCK_ALERT_TECHNICAL_WEIGHT` | `0.25` | Technical signal weight |
| `STOCK_ALERT_NEWS_WEIGHT` | `0.15` | News signal weight |
| `STOCK_ALERT_PAPER_CASH` | `100000.0` | Paper portfolio starting cash |
| `STOCK_ALERT_AUTH_SECURE` | `0` | Set `1` when serving over HTTPS |
| `STOCK_ALERT_REFRESH_FAST` / `STOCK_ALERT_REFRESH_SLOW` | `300` / `1800` | Background refresh cadence (seconds) |

Weights and thresholds are fully documented in `src/stock_alert_app/config.py`.

---

## API overview

The backend exposes a JSON API under `/api`. Highlights:

| Group | Endpoints |
| --- | --- |
| Auth | `POST /api/auth/register`, `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me` |
| Markets & search | `GET /api/markets`, `GET /api/search`, `GET /api/ticker-strip` |
| Analysis | `GET /api/dossier`, `GET /api/analyze`, `GET /api/verdicts`, `GET /api/news` |
| Universe | `GET /api/scanner`, `GET /api/screener`, `POST /api/refresh`, `GET /api/refresh/status` |
| Watchlist | `GET/POST /api/watchlist`, `DELETE /api/watchlist` |
| Price alerts | `GET/POST /api/price-alerts`, `PATCH/DELETE /api/price-alerts/{id}` |
| Charts & indices | `GET /api/chart/{market}/{ticker}`, `GET /api/indexes`, `GET /api/indexes/{symbol}/history` |
| Paper trading | `GET /api/paper/portfolio`, `POST /api/paper/order`, `GET /api/paper/stats`, `GET /api/paper/equity`, `GET /api/paper/leaderboard`, … |
| Simulation | `POST /api/simulate` |
| LSTM | `GET /api/lstm/batch-predict`, `GET /api/lstm/train` |
| Institutional | `GET /api/funds`, `GET /api/funds/{cik}`, `POST /api/funds/refresh` |
| Research | `GET /api/agent`, `GET /api/reddit` |
| Notifications | `GET /api/notifications`, `POST /api/notifications/scan`, `POST /api/notifications/ack` |

---

## Testing

```bash
# Backend (pytest)
uv run pytest

# Frontend (node:test)
cd frontend
npm test
```

---

## Disclaimer

StockVerdict provides automated analysis for educational and research purposes only.
It is not financial advice. Verdicts are generated from historical data and statistical
models and carry no guarantee of accuracy or future performance. The paper-trading
module is a simulation and never interacts with a real brokerage.
