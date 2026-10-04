# Deriv Signal Lab v6

FastAPI market analytics dashboard with directional BUY/SELL/WAIT signals and experimental Digit Matches analytics for Deriv markets. Deployed on Vercel.

## Features
- **Market Signals:** EMA20/50, MACD, RSI14, Bollinger Bands and ATR combined into a BUY / SELL / WAIT signal per market, valid for the selected candle window (1 min to 1 hour).
- **Digit Matches:** estimates probabilities for last digits 0–9 from recent tick history (frequency, recency weighting, first-order transitions, small overdue component). A MATCH is only shown when the probability separation and a walk-forward backtest both pass conservative gates.

## Rate-limit protection (v6)
Deriv allows 220 calls/minute (14,400/hour) for this API group. The app stays well below that:

- **Browser:** loads markets gradually, at most 8 every 12 seconds and 40 per minute. On a rate-limit reply it pauses about 60 s, halves its pace, then recovers slowly. Hidden tabs stop making requests. Signals refresh only when their candle closes; digit rows refresh about every 2 minutes.
- **Server:** at most `DERIV_MAX_CALLS_PER_MIN` Deriv calls per minute (default 90), and at most 12 symbols per request. On any `RateLimit` reply it stops immediately and cools down for 65 s. Results are cached per symbol so multiple viewers share them.

## API
| Endpoint | Purpose |
|---|---|
| `GET /` | Dashboard |
| `GET /api/symbols` | Active Deriv symbols (cached 10 min) |
| `GET /api/market-board?granularity=300&symbols=A,B` | Signals for up to 12 symbols |
| `GET /api/digit-board?count=1000&symbols=A,B` | Digit analytics for up to 12 symbols |
| `GET /api/signal/{symbol}` / `GET /api/digits/{symbol}` | Single symbol |
| `GET /api/limits` | Current server call budget and cooldown |
| `GET /api/health` | Health check |

## Configuration
| Variable | Default | Meaning |
|---|---|---|
| `DERIV_MAX_CALLS_PER_MIN` | `90` | Server-side cap on Deriv calls per minute |

## Run locally
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000

## Deploy on Vercel
The repo root contains `index.py`, which exposes `app.main:app`. Import the repo into Vercel with the **FastAPI** framework preset; no build settings are needed.

## Disclaimer
This is an experimental analytical and educational tool. Its signals and digit estimates are not guaranteed predictions or trading recommendations. A random 0–9 digit baseline is 10%, and past accuracy does not guarantee future results.
