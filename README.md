# Deriv Signal Lab v7

FastAPI market analytics dashboard with directional BUY/SELL/WAIT signals and experimental Digit Matches analytics for Deriv markets. Deployed on Vercel.

## Features
- **Market Signals:** EMA20/50, MACD, RSI14, Bollinger Bands and ATR combined into a BUY / SELL / WAIT signal per market, valid for the selected candle window (1 min to 1 hour).
- **Digit Matches:** estimates probabilities for last digits 0–9 from recent tick history (frequency, recency weighting, first-order transitions, small overdue component). A MATCH is only shown when the probability separation and a walk-forward backtest both pass conservative gates.

## Bulk Matches trading (v7)
On the **Digit Matches** tab you can place several Digit Matches contracts at the same moment.

- **Connect:** enter your Deriv App ID and a Personal Access Token with the `trade` scope (both from developers.deriv.com). The token is kept only in the open tab's memory. The **Demo** account is selected by default; **Real** requires ticking a risk acknowledgement and confirming each run.
- **Settings:** number of bulk trades (1–10), stake per trade, duration in ticks, number of rounds, pause between rounds, and whether to use the top markets by edge or only MATCH-flagged markets.
- **How a round works:** predictions for the top synthetic markets are recalculated, then one `DIGITMATCH` contract is bought on each chosen market, all at once, using that market's predicted digit. The next round starts only after every contract has settled.
- **Limits:** a max stake per trade, and a daily loss limit (tracked per account in the browser) that stops a round before it could exceed the limit.
- **Connection:** accounts and the one-time WebSocket URL are requested from Deriv directly by the browser. If the browser blocks that, it falls back to `/api/trade/accounts` and `/api/trade/otp`, which forward the token to Deriv for that single request without storing or logging it.

Each Matches contract wins only if the last digit equals the chosen digit, about 1 in 10, and the payout is below the 10× needed to break even. Bulk trading increases the amount at risk; it does not improve the odds.

## Rate-limit protection (v6)
Deriv allows 220 calls/minute (14,400/hour) for this API group. The app stays well below that:

- **Browser:** loads markets gradually, at most 8 every 12 seconds and 40 per minute. On a rate-limit reply it pauses about 60 s, halves its pace, then recovers slowly. Hidden tabs stop making requests. Signals refresh only when their candle closes; digit rows refresh about every 2 minutes.
- **Server:** at most `DERIV_MAX_CALLS_PER_MIN` Deriv calls per minute (default 90), and at most 12 symbols per request. On any `RateLimit` reply it stops immediately and cools down for 65 s. Results are cached per symbol so multiple viewers share them.

## Bulk Matches trading (v7)
On the **Digit Matches** tab, the Bulk Matches Trading panel places several DIGITMATCH contracts at the same moment.
- **Demo / Real switch** (Demo by default). Connect with a Deriv Personal Access Token (trade scope) and your Deriv App ID. The app checks that Deriv returned the matching demo/real endpoint before trading.
- **Trades at once:** 1–12, each on a different market using its freshly recomputed predicted digit (MATCH-flagged only, or top by edge). Stake per trade and duration in ticks are configurable.
- **Safeguards:** max stake per trade, a daily loss limit per account type, a confirmation dialog showing the total at risk, and an extra acknowledgement for real-money trades.
- **Token handling:** trading runs in the browser over the Deriv WebSocket. If the browser cannot reach Deriv's REST API directly, the token passes through `/api/deriv/*` to `api.derivws.com` without being logged or stored.

Each Matches contract has roughly a 1-in-10 chance and pays less than 10× the stake, so the expected result of every trade is a loss.

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
| `POST /api/trade/accounts`, `POST /api/trade/otp` | Relay for Deriv account list and trading WebSocket URL (fallback only) |

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
