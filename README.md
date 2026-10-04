# Deriv Signal Lab v5 — rate-aware auto trader

Experimental Deriv Options dashboard with OAuth2 PKCE account connection, persistent browser WebSocket streaming, local tick analysis, configurable market selection, and Start/Stop automated execution.

## Safety defaults
- Demo-first: real-money accounts require an explicit confirmation checkbox before Start.
- Fixed stake only. No martingale/chasing logic.
- Session stop-loss, profit target, max trades, cooldown, max concurrent contracts.
- STOP prevents new buys while existing contracts continue to settlement.
- App-side trading request limiter defaults to 180/minute, below Deriv's documented 360/minute shared budget for proposal/open-contract/buy/sell.

## Connecting a Deriv account
Two methods are supported:

1. **API token (works immediately).** In Deriv go to Settings → API token, create a token with the `trade` scope, then paste it into the dashboard together with your Deriv App ID (or set `DERIV_APP_ID` so it is pre-filled). Tokens are stored encrypted in an httponly cookie for 12 hours and are never persisted server-side.
2. **Deriv login (OAuth2 PKCE).** Register an application at developers.deriv.com with the callback URL set exactly to your app root, then set `DERIV_CLIENT_ID` (the App ID) and `DERIV_REDIRECT_URI`.

## Deriv OAuth setup
Register an OAuth2 app in Deriv and set its callback URL exactly to your app root (for local use `http://localhost:8000/`). The app requests only the `trade` scope.

Environment:
```
DERIV_CLIENT_ID=...
DERIV_REDIRECT_URI=http://localhost:8000/
SESSION_SECRET=<long random value>
DERIV_APP_ID=...   # optional; defaults to DERIV_CLIENT_ID, used for API-token login
```

## Local run
```
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Vercel
Set the same three environment variables in Vercel. Change `DERIV_REDIRECT_URI` to the production HTTPS URL and register that exact URL in Deriv OAuth settings.

## Trading modes
- Specific: scan only checked symbols and choose the strongest eligible signal.
- Dynamic: scan subscribed symbols and rank by the selected model's current edge.
- Random eligible: randomly choose among symbols that already pass the configured signal gate. It does **not** assume random selection is profitable.

Strategies implemented: Digit Match, Digit Differs, Digit Over, Digit Under, Even, Odd, Rise, Fall. Contract availability can vary by symbol; proposal errors are treated as ineligible and do not trigger a buy.

This is experimental software, not a guarantee of profit. Validate on a demo account before enabling real-money trading.
