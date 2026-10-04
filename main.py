from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path
import asyncio, json, math, os, re, time
import urllib.request, urllib.error
from pydantic import BaseModel
from collections import deque
import numpy as np
import pandas as pd
import websockets

WS = "wss://api.derivws.com/trading/v1/options/ws/public"
POOL_SIZE = 2          # websocket connections per request
RPC_TIMEOUT = 20
# Deriv allows 220 calls/min & 14,400/hour for this group. We stay far below it:
MAX_CALLS_PER_MIN = int(os.getenv("DERIV_MAX_CALLS_PER_MIN", "90"))
MAX_BATCH = 12         # most symbols one board request may fetch
RATE_LIMIT_COOLDOWN = 65
INDEX_HTML = Path(__file__).with_name("index.html")

app = FastAPI(title="Deriv Signal Lab", version="0.3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    # Always return JSON so the dashboard can show a readable message.
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})

# ---------- helpers ----------
def clean(o):
    """Make any result JSON-safe: numpy -> python, NaN/inf -> None."""
    if isinstance(o, dict): return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)): return [clean(v) for v in o]
    if isinstance(o, np.ndarray): return [clean(v) for v in o.tolist()]
    if isinstance(o, (bool, np.bool_)): return bool(o)
    if isinstance(o, np.integer): return int(o)
    if isinstance(o, (float, np.floating)):
        f = float(o); return f if math.isfinite(f) else None
    return o

def err_text(e):
    return getattr(e, "detail", None) or str(e) or type(e).__name__

_cache = {}
def cache_get(key, ttl):
    hit = _cache.get(key)
    return hit[1] if hit and time.time() - hit[0] < ttl else None
def cache_set(key, val):
    _cache[key] = (time.time(), val); return val

# ---------- Deriv websocket access ----------
class Deferred(Exception):
    """Request was not sent to Deriv (budget exhausted or cooling down after a rate limit)."""

_calls = deque()            # timestamps of Deriv calls made by this server instance
_blocked_until = 0.0        # set when Deriv answers with RateLimit

def cooldown_left():
    return max(0.0, _blocked_until - time.time())

def take_budget(n):
    """Reserve up to n calls inside the per-minute budget. Returns how many may be sent."""
    now = time.time()
    while _calls and now - _calls[0] > 60: _calls.popleft()
    if cooldown_left() > 0: return 0
    allowed = max(0, min(n, MAX_CALLS_PER_MIN - len(_calls)))
    _calls.extend([now] * allowed)
    return allowed

async def rpc_batch(payloads, pool=POOL_SIZE):
    """Send requests over a few shared connections, within budget.
    Returns a list of dicts or Exceptions (Deferred = not sent)."""
    global _blocked_until
    n_ok = take_budget(len(payloads))
    results = [None] * len(payloads)
    for i in range(n_ok, len(payloads)):
        results[i] = Deferred("Waiting for rate-limit budget" if cooldown_left() == 0 else "Deriv rate limit cooldown")
    queue = asyncio.Queue()
    for i in range(n_ok): queue.put_nowait(i)
    stop = {"flag": False}

    async def worker():
        global _blocked_until
        ws = None
        try:
            while not queue.empty():
                i = queue.get_nowait()
                if stop["flag"]:
                    results[i] = Deferred("Deriv rate limit cooldown"); continue
                payload = {**payloads[i], "req_id": i + 1}
                for attempt in range(2):
                    try:
                        if ws is None:
                            ws = await websockets.connect(WS, ping_interval=20, ping_timeout=20, open_timeout=15, max_size=2**24)
                        await ws.send(json.dumps(payload))
                        while True:
                            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=RPC_TIMEOUT))
                            if msg.get("req_id") in (None, payload["req_id"]): break
                        err = msg.get("error")
                        if err and (err.get("code") == "RateLimit" or "rate limit" in str(err.get("message", "")).lower()):
                            # Stop immediately: never keep hammering once Deriv says slow down.
                            _blocked_until = time.time() + RATE_LIMIT_COOLDOWN
                            stop["flag"] = True
                            results[i] = Deferred("Deriv rate limit cooldown")
                        elif err:
                            results[i] = HTTPException(502, err.get("message", "Deriv API error"))
                        else:
                            results[i] = msg
                        break
                    except Exception as e:
                        if ws is not None:
                            try: await ws.close()
                            except Exception: pass
                        ws = None
                        results[i] = HTTPException(502, f"Deriv connection problem: {type(e).__name__}: {e}")
                        await asyncio.sleep(1)
        finally:
            if ws is not None:
                try: await ws.close()
                except Exception: pass

    if n_ok:
        await asyncio.gather(*(worker() for _ in range(max(1, min(pool, n_ok)))))
    return results

async def rpc(payload):
    res = (await rpc_batch([payload], 1))[0]
    if isinstance(res, Deferred): raise HTTPException(429, f"{res} - retry in {round(cooldown_left()) or 60}s")
    if isinstance(res, Exception): raise res
    return res

async def active_symbols():
    cached = cache_get("symbols", 600)
    if cached is not None: return cached
    if cooldown_left() > 0 and _cache.get("symbols"): return _cache["symbols"][1]
    data = await rpc({"active_symbols": "brief"})
    out, seen = [], set()
    for x in data.get("active_symbols", []):
        symbol = x.get("underlying_symbol") or x.get("symbol")
        if not symbol or symbol in seen: continue
        seen.add(symbol)
        out.append({"symbol": symbol, "name": x.get("underlying_symbol_name") or x.get("display_name") or symbol,
                    "market": x.get("market") or "Other", "submarket": x.get("submarket")})
    if not out: raise HTTPException(502, "Deriv returned no active symbols")
    return cache_set("symbols", out)

# ---------- directional signals ----------
def rsi(s, n=14):
    d = s.diff(); up = d.clip(lower=0).rolling(n).mean(); dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    flat = pd.Series(np.where(up > 0, 100.0, 50.0), index=s.index)   # no down moves: 100 (or 50 if no movement at all)
    return out.where(dn != 0, flat).where(up.notna() & dn.notna())

def enrich(df):
    c = df.close.astype(float)
    df["ema20"] = c.ewm(span=20, adjust=False).mean(); df["ema50"] = c.ewm(span=50, adjust=False).mean()
    df["rsi14"] = rsi(c)
    e12 = c.ewm(span=12, adjust=False).mean(); e26 = c.ewm(span=26, adjust=False).mean()
    df["macd"] = e12 - e26; df["macd_signal"] = df.macd.ewm(span=9, adjust=False).mean()
    mid = c.rolling(20).mean(); sd = c.rolling(20).std(); df["bb_upper"] = mid + 2 * sd; df["bb_lower"] = mid - 2 * sd
    tr = pd.concat([(df.high - df.low).abs(), (df.high - c.shift()).abs(), (df.low - c.shift()).abs()], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()
    return df

def score_signal(row):
    score = 0.0; reasons = []
    if row.ema20 > row.ema50: score += 1; reasons.append("EMA20 above EMA50")
    else: score -= 1; reasons.append("EMA20 below EMA50")
    if row.macd > row.macd_signal: score += 1; reasons.append("MACD bullish")
    else: score -= 1; reasons.append("MACD bearish")
    if row.rsi14 < 35: score += 0.8; reasons.append("RSI near oversold")
    elif row.rsi14 > 65: score -= 0.8; reasons.append("RSI near overbought")
    else: reasons.append("RSI neutral")
    if row.close < row.bb_lower: score += 0.6; reasons.append("Below lower Bollinger band")
    elif row.close > row.bb_upper: score -= 0.6; reasons.append("Above upper Bollinger band")
    prob_up = 1 / (1 + math.exp(-score)); confidence = max(prob_up, 1 - prob_up)
    signal = "BUY" if prob_up >= 0.68 else "SELL" if prob_up <= 0.32 else "WAIT"
    return signal, prob_up, confidence, reasons

def candles_payload(symbol, granularity, count):
    return {"ticks_history": symbol, "end": "latest", "count": max(80, min(count, 1000)), "style": "candles", "granularity": granularity}

def analyze_data(symbol, granularity, data):
    candles = data.get("candles") or []
    if len(candles) < 60: raise HTTPException(422, "Not enough candle history returned")
    df = pd.DataFrame(candles).rename(columns={"epoch": "time"})
    for k in ["open", "high", "low", "close"]: df[k] = pd.to_numeric(df[k], errors="coerce")
    df = enrich(df).dropna()
    if df.empty: raise HTTPException(422, "Indicators could not be calculated")
    row = df.iloc[-1]
    sig, pup, conf, reasons = score_signal(row)
    atr = float(row.atr14); price = float(row.close)
    invalid = target = None
    if sig == "BUY": invalid = price - 1.5 * atr; target = price + 2 * atr
    elif sig == "SELL": invalid = price + 1.5 * atr; target = price - 2 * atr
    candle_start = int(row.time); valid_until = candle_start + granularity; now = int(time.time())
    if valid_until <= now: valid_until = ((now // granularity) + 1) * granularity
    return {"symbol": symbol, "granularity": granularity, "signal": sig, "probability_up": round(pup, 4), "confidence": round(conf, 4),
            "price": price, "invalidation": invalid, "target": target, "valid_from": candle_start, "valid_until": valid_until,
            "seconds_remaining": max(0, valid_until - now),
            "indicators": {"rsi14": round(float(row.rsi14), 2), "ema20": float(row.ema20), "ema50": float(row.ema50),
                           "macd": float(row.macd), "macd_signal": float(row.macd_signal), "atr14": atr},
            "reasons": reasons,
            "notice": "Experimental analytical signal. The signal is recalculated when its candle window changes; it is not a guaranteed prediction or investment recommendation."}

# ---------- digit analytics ----------
def extract_digits(prices):
    vals = []
    for p in prices:
        s = str(p).rstrip('0').rstrip('.') if '.' in str(p) else str(p)
        ds = [c for c in s if c.isdigit()]
        if ds: vals.append(int(ds[-1]))
    return vals

def digit_features(vals, window=500):
    v = np.asarray(vals[-window:], dtype=np.int64); n = len(v)
    counts = np.bincount(v, minlength=10).astype(float)
    freq = (counts + 1) / (counts.sum() + 10)
    # Exponentially weight recent observations.
    rec = np.bincount(v, weights=np.exp(np.linspace(-3, 0, n)), minlength=10) if n else np.zeros(10)
    rec = (rec + 0.05) / (rec.sum() + 0.5)
    # First-order transition: what usually follows the current last digit?
    trans = np.ones((10, 10))
    if n > 1: np.add.at(trans, (v[:-1], v[1:]), 1)
    tprob = trans[v[-1]] / trans[v[-1]].sum() if n else np.ones(10) / 10
    # Streak / overdue component: small, bounded contribution only.
    last = np.full(10, -1)
    if n: np.maximum.at(last, v, np.arange(n))
    gaps = np.where(last >= 0, n - 1 - last, n)
    overdue = gaps.astype(float) + 1; overdue = overdue / overdue.sum()
    # Ensemble intentionally conservative: frequency is not assumed predictive by itself.
    probs = .30 * freq + .35 * rec + .30 * tprob + .05 * overdue
    probs = probs / probs.sum()
    entropy = float(-(probs * np.log2(np.clip(probs, 1e-12, None))).sum())
    return probs, freq, rec, tprob, [int(g) for g in gaps], entropy

def walk_forward_backtest(vals, min_train=150, test_points=250):
    if len(vals) < min_train + 30: return {"trials": 0, "hits": 0, "accuracy": None, "baseline": 0.1, "lift": None}
    start = max(min_train, len(vals) - test_points)
    hits = 0; trials = 0; confs = []
    for i in range(start, len(vals)):
        hist = vals[:i]
        probs, *_ = digit_features(hist, min(500, len(hist)))
        pred = int(np.argmax(probs)); hits += int(pred == vals[i]); trials += 1; confs.append(float(probs[pred]))
    acc = hits / trials if trials else None
    return {"trials": trials, "hits": hits, "accuracy": round(acc, 4) if acc is not None else None, "baseline": 0.1,
            "lift": round(acc / 0.1, 3) if acc is not None else None,
            "avg_model_probability": round(float(np.mean(confs)), 4) if confs else None}

def ticks_payload(symbol, count):
    return {"ticks_history": symbol, "end": "latest", "count": max(250, min(count, 5000)), "style": "ticks"}

def digit_data(symbol, data):
    hist = data.get("history") or {}; prices = hist.get("prices") or []; times = hist.get("times") or []
    vals = extract_digits(prices)
    if len(vals) < 100: raise HTTPException(422, "Not enough tick history returned")
    probs, freq, recency, transition, gaps, entropy = digit_features(vals, min(1000, len(vals)))
    rec = int(np.argmax(probs)); top = float(probs[rec]); second = float(np.partition(probs, -2)[-2]); edge = top - second
    bt = walk_forward_backtest(vals)
    # Opportunity gate: require both model separation and observed walk-forward performance.
    acc = bt.get("accuracy") or 0
    threshold = max(0.125, second + 0.012)
    opportunity = bool(top >= threshold and edge >= 0.012 and bt.get("trials", 0) >= 100 and acc >= 0.105)
    strength = "HIGH" if opportunity and top >= 0.15 and acc >= 0.12 else "WATCH" if opportunity else "NO MATCH"
    now = time.time(); intervals = [b - a for a, b in zip(times[-80:-1], times[-79:]) if b > a]
    interval = float(np.median(intervals)) if intervals else 1.0; last_epoch = float(times[-1]) if times else now
    next_tick = max(now, last_epoch) + max(.25, interval)
    return {"symbol": symbol, "recommended_digit": rec, "probabilities": [round(float(x), 5) for x in probs],
            "frequency_probabilities": [round(float(x), 5) for x in freq], "recency_probabilities": [round(float(x), 5) for x in recency],
            "transition_probabilities": [round(float(x), 5) for x in transition], "gaps": gaps, "sample_size": len(vals),
            "last_digit": vals[-1], "last_price": prices[-1], "entropy": round(entropy, 4), "edge": round(edge, 5),
            "threshold": round(threshold, 5), "opportunity": opportunity, "strength": strength, "backtest": bt,
            "estimated_tick_interval": round(interval, 3), "next_tick_estimate": next_tick,
            "seconds_to_estimated_tick": round(max(0, next_tick - now), 3), "generated_at": now,
            "notice": "Experimental statistical model using frequency, recency and first-order transitions. MATCH is shown only when the current probability separation and walk-forward test pass conservative gates. Historical accuracy does not guarantee future results."}

# ---------- routes ----------
@app.get("/", response_class=HTMLResponse)
async def home(): return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))

@app.get("/api/health")
async def health(): return {"ok": True, "time": time.time()}

@app.get("/api/symbols")
async def symbols(): return await active_symbols()

@app.get("/api/signal/{symbol}")
async def signal(symbol: str, granularity: int = 300, count: int = 300):
    return clean(analyze_data(symbol, granularity, await rpc(candles_payload(symbol, granularity, count))))

@app.get("/api/digits/{symbol}")
async def digits(symbol: str, count: int = 1000):
    data = await rpc(ticks_payload(symbol, count))
    return clean(await asyncio.to_thread(digit_data, symbol, data))

# per-symbol result cache (shared by all visitors on this instance)
_row_cache = {}

def _pick_symbols(all_syms, symbols):
    by = {m["symbol"]: m for m in all_syms}
    if symbols:
        wanted = [x.strip() for x in symbols.split(",") if x.strip()]
        return [by.get(x, {"symbol": x, "name": x, "market": "Other", "submarket": None}) for x in wanted][:MAX_BATCH]
    return all_syms[:MAX_BATCH]

async def _board(kind, metas, param, payload_fn, compute_fn, ttl_fn, fresh=False):
    now = time.time()
    rows, todo = {}, []
    for meta in metas:
        hit = None if fresh else _row_cache.get((kind, meta["symbol"], param))
        if hit and hit[0] > now: rows[meta["symbol"]] = hit[1]
        else: todo.append(meta)
    raw = await rpc_batch([payload_fn(m["symbol"]) for m in todo]) if todo else []
    used = sum(1 for r in raw if not isinstance(r, Deferred))

    def compute():
        out = {}
        for meta, data in zip(todo, raw):
            try:
                if isinstance(data, Deferred):
                    out[meta["symbol"]] = {**meta, "deferred": True, "error": str(data)}; continue
                if isinstance(data, Exception): raise data
                d = compute_fn(meta["symbol"], data)
                d.update({"name": meta["name"], "market": meta["market"], "submarket": meta.get("submarket")})
                _row_cache[(kind, meta["symbol"], param)] = (ttl_fn(d), clean(d))
            except Exception as e:
                d = {**meta, "error": err_text(e)}
                if kind == "mb": d.update({"signal": "N/A", "granularity": param})
            out[meta["symbol"]] = d
        return out

    rows.update(await asyncio.to_thread(compute))
    ordered = [rows[m["symbol"]] for m in metas]
    return clean({"generated_at": time.time(), "count": len(ordered), "markets": ordered,
                  "deriv_calls": used, "rate_limited": cooldown_left() > 0,
                  "retry_after": round(cooldown_left()), "max_calls_per_min": MAX_CALLS_PER_MIN})

@app.get("/api/market-board")
async def market_board(granularity: int = 300, symbols: str = ""):
    metas = _pick_symbols(await active_symbols(), symbols)
    res = await _board("mb", metas, granularity,
                       lambda s: candles_payload(s, granularity, 220),
                       lambda s, d: analyze_data(s, granularity, d),
                       lambda d: d["valid_until"] + 2)          # reuse until the candle closes
    res["granularity"] = granularity
    return res

@app.get("/api/digit-board")
async def digit_board(count: int = 1000, symbols: str = "", fresh: int = 0):
    metas = _pick_symbols(await active_symbols(), symbols)
    return await _board("db", metas, count,
                        lambda s: ticks_payload(s, count),
                        digit_data,
                        lambda d: time.time() + 45,             # digit stats reused for 45s
                        fresh=bool(fresh))                      # fresh=1: recompute right before a bulk trade

@app.get("/api/limits")
async def limits():
    now = time.time()
    while _calls and now - _calls[0] > 60: _calls.popleft()
    return {"max_calls_per_min": MAX_CALLS_PER_MIN, "used_last_minute": len(_calls),
            "cooldown_seconds": round(cooldown_left()), "max_batch": MAX_BATCH}

# ---------- trading relay (only used if the browser cannot call Deriv REST directly) ----------
# The token is forwarded to Deriv for this single request and is never stored or logged.
DERIV_REST = "https://api.derivws.com"

class TradeAuth(BaseModel):
    token: str
    app_id: str
    account_id: str | None = None

def _deriv_rest(method, path, token, app_id):
    req = urllib.request.Request(DERIV_REST + path, method=method, data=b"{}" if method == "POST" else None,
                                 headers={"Authorization": f"Bearer {token}", "Deriv-App-ID": app_id,
                                          "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try: data = json.loads(raw or b"{}")
        except Exception: data = {"detail": raw.decode("utf-8", "replace")[:300] or f"HTTP {e.code}"}
        return e.code, data

def _check(auth: TradeAuth):
    if not auth.token.strip() or not re.fullmatch(r"[\w-]{1,40}", auth.app_id.strip()):
        raise HTTPException(400, "A token and a valid App ID are required")

@app.post("/api/trade/accounts")
async def trade_accounts(auth: TradeAuth):
    _check(auth)
    code, data = await asyncio.to_thread(_deriv_rest, "GET", "/trading/v1/options/accounts", auth.token.strip(), auth.app_id.strip())
    return JSONResponse(status_code=code, content=data, headers={"Cache-Control": "no-store"})

@app.post("/api/trade/otp")
async def trade_otp(auth: TradeAuth):
    _check(auth)
    if not auth.account_id or not re.fullmatch(r"\w{1,40}", auth.account_id):
        raise HTTPException(400, "Invalid account id")
    code, data = await asyncio.to_thread(_deriv_rest, "POST", f"/trading/v1/options/accounts/{auth.account_id}/otp", auth.token.strip(), auth.app_id.strip())
    return JSONResponse(status_code=code, content=data, headers={"Cache-Control": "no-store"})
