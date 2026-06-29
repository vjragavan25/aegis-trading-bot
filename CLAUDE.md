# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Aegis is a fully automated AI-powered crypto trading bot running on Binance Demo (testnet). It scans BTC/USDT, ETH/USDT, and SOL/USDT on 15-minute cycles, applies an 8-condition entry gate, and places trades with OCO (One-Cancels-the-Other) stop-loss/take-profit orders.

## Running the System

Two terminals required — server must start first:

```cmd
cd "C:\Users\Vijay\Downloads\AI Analyst\Run"

# Terminal 1
python aegis_server.py

# Terminal 2
python aegis_bot.py
```

Useful URLs while running:
- `http://localhost:8888/status` — circuit breaker state, open positions, daily PnL
- `http://localhost:8888/balance` — live account balance from Binance
- `http://localhost:8888/reset` — clear circuit breaker after manual review
- `http://localhost:8888/checkclosures` — manually trigger OCO fill detection
- `http://localhost:8888/logs` — last 100 lines from both server and bot logs
- Open `aegis_journal.html` in browser for the trade dashboard

## Running Tests

```cmd
python test_aegis.py
```

Run tests before every deployment. All 56 tests must pass. Tests run fully offline (no network, no real files).

## Architecture

**Two-process separation** is intentional and must be maintained:

- `aegis_server.py` — Local signing bridge on port 8888. Holds Binance API keys, does HMAC-SHA256 signing, enforces server-side safety limits (circuit breaker, position caps, per-asset concentration), executes trades via OCO orders.

- `aegis_bot.py` — Scanner process. Fetches market data from Binance public endpoints, calculates signals, runs the 8-condition entry gate (`should_trade()`), calls the server via HTTP for validation and trade execution.

- `aegis_ai.py` — Read-only commentator. Calls Claude API for signal reasoning, cycle briefs, morning briefs, and anomaly detection. **The AI never decides whether to trade — only narrates decisions the deterministic gate has already made.** If the Claude API is down, worst case is a missing explanation, never a wrong trade. Keep this separation permanent.

## Entry Gate (8 conditions — all must pass)

**Bot-side (`should_trade()` in aegis_bot.py):**
- `opp_score >= 68`
- `conf_score >= 65`
- `risk_score <= 50`
- `volume_ratio >= 1.2x` (vs 20-period average)
- `tf_bullish >= 2` (out of 3 timeframes: 1H, 4H, Daily)
- `regime_quality`: current regime must be "Strong bull"
- `regime_consistency`: previous scan cycle for same symbol must also be "Strong bull" — prevents entering at regime transition peaks; defaults to block on first cycle after restart
- `daily_trend`: 1D close must be above 50-period daily SMA — added after backtest found 0% win rate in Feb 2025 when price was below daily SMA50

**Server-side (`validate_signal()` in aegis_server.py):**
- Same 5 score/volume/TF checks
- `MAX_OPEN_POSITIONS_PER_ASSET = 1` — blocks stacking into the same symbol
- Circuit breaker not tripped

## Key Safety Constraints

**Never remove these safeguards:**
1. **Symbol match check** (`calc_signals()`): verifies `ticker["symbol"] == requested symbol`. Added after first trade executed as BTCUSDT when a SOLUSDT signal was requested (demo API returned mismatched ticker data).
2. **Price sanity check** (`calc_signals()`): ticker price vs 1H kline close must be within 5%. Catches data source disagreement.
3. **Position value cap** (`calc_position_size()`): `MAX_POSITION_VALUE_PCT = 5%` of balance per trade regardless of risk math. Guards against price mismatch causing runaway position sizing.
4. **OCO orders** (`place_trade()`): SL + TP placed as a linked pair via `POST /orderList/oco`. Fallback to SL-only if OCO fails. If SL also fails, circuit breaker trips immediately.
5. **Circuit breaker**: auto-trips on unprotected position, daily loss >5%, or max 10 trades/day. Reset via `http://localhost:8888/reset` after manual review only.
6. **State hydration** (`hydrate_state_from_csv()`): runs at server startup to restore open positions from CSV so circuit breaker and concentration checks work correctly after restart.

## Binance API Notes

- **REST base**: `https://demo-api.binance.com/api/v3` — this is Binance Demo Mode, NOT the old testnet.
- **OCO endpoint** (post Aug-2025 Binance migration): `POST /api/v3/orderList/oco` with `aboveType`/`belowType` params (not the old single `type` param). Above leg = `LIMIT_MAKER` (TP), below leg = `STOP_LOSS_LIMIT` (SL).
- If signed endpoints start returning -2015 while public endpoints work, Binance has changed the demo API domain again — check official docs and update `REST_BASE` (server) and `BINANCE_REST` (bot).
- LOT_SIZE and PRICE_FILTER precision is fetched per-symbol from `/exchangeInfo` and cached. All quantities round DOWN to lot step; all prices round DOWN to tick size.
- Fee deduction: BUY MARKET fills may deduct commission in the base asset (e.g. ETH fee on ETHUSDT buy). Use `net_qty = executedQty - base_asset_fees_from_fills`, not raw `executedQty`, for OCO/SL sizing.

## Data Files

Auto-generated at runtime — do not edit manually except for manual reconciliation:
- `aegis_trades.csv` — 25-column trade log (auto-migrates old 21-column schema on write)
- `aegis_signals.csv` — every 15-min scan result
- `aegis_events.txt` — server log
- `aegis_bot_YYYY-MM-DD.log` — bot daily log, 7-day retention
- `aegis_ai_briefs.txt` — hourly AI market briefs
- `aegis_ai_log.json` — AI trade commentary (last 500 entries)

Reference (completed backtests):
- `aegis_backtest_v2_report.txt` — Run #2: 315 trades, 24 months, +0.269R expectancy, 9.3% max drawdown

## Risk Configuration (aegis_bot.py)

```python
STOP_LOSS_PCT   = 0.025   # 2.5% below entry
TAKE_PROFIT_PCT = 0.055   # 5.5% above entry → 2.2:1 R:R
RISK_PER_TRADE_PCT = 1.0  # 1% of balance per trade (in server)
```

## Scoring Weights (calc_signals)

| Factor | Weight |
|--------|--------|
| Trend (SMA20 vs SMA50, 1H) | 25% |
| Momentum (24h % change) | 22% |
| Timeframe alignment (1H/4H/D) | 20% |
| Volume quality (vs 20-period avg) | 18% |
| Order book bias (bid/ask imbalance) | 15% |

## Live Readiness Gate

Do not move to live trading before: 30+ closed OCO-driven trades, 55%+ win rate, 2:1+ avg R:R, <15% max drawdown, 0 unresolved circuit breaker trips in last 10 trades. 60-day moratorium expires 2026-08-12.

## Session Continuity

Upload `AEGIS_PROJECT_LOG.md` at the start of each new chat session — it contains the full incident history, all bugs fixed, and current system state.

---

## Token Optimization — Always On

Token efficiency is a first-class priority in every response. This applies to the entire session without exception.

### Response rules

**Code**
- Write complete, working code. No placeholders.
- On edits, show only changed lines with minimal surrounding context.
- Comments only when logic is genuinely non-obvious.

**Explanations**
- Lead with the answer. No preamble, no closing summary.
- Never start with "Sure!", "Great!", "Certainly!", or any filler opener.
- Cut any sentence that restates what was just said.

**Clarifications**
- State your assumption and proceed when something is ambiguous.
- One clarifying question max, only when ambiguity would cause wrong output.

**Errors & debugging**
- Root cause in one sentence. Show the fix. Skip the walkthrough unless asked.

**Repetition**
- Never restate the user's question.
- Never repeat code or decisions already established in this session.

### Before every response, check:
- Can this be shorter without losing meaning? → Shorten it.
- Is this sentence adding new information? → If not, cut it.
- Is this code already in context? → Do not rewrite it.

### Exceptions — always be thorough for:
- Security or data-loss risks
- Explicitly requested detailed explanations
- User-facing documentation or README files