# PARKED IDEA: "Reversal Short Engine — Observation Phase"

**Status:** Design complete, implementation pending
**Parked:** 2026-06-25
**Resume trigger:** Next available Claude Code session after spot system is stable
**Do NOT merge into AEGIS_PROJECT_LOG.md** — keep as separate reference
**Reopen with:** "Reversal Short Engine" or "observation phase"

---

## The Idea

A parallel observation-only module that watches a futures asset watchlist for
strong upward moves (top performers), detects reversal signals, and logs every
potential short entry with subsequent price action — building a learning file
over time. No trades, no orders, pure observation.

The goal: develop genuine conviction in a reversal-short strategy through
evidence before committing any capital. The learning file becomes the foundation
for a backtest, which becomes the foundation for demo futures trading, which
becomes the foundation for real futures trading.

**Sequencing (non-negotiable):**
1. Observe and log (4-6 weeks, 30-50 observations)
2. Review learning file — which signal combinations had highest hit rate?
3. Backtest validated signals against historical data
4. Demo futures testnet — validate order mechanics, not just signal logic
5. Real futures — only after demo confirms edge

---

## Why This Strategy Makes Sense

- Top-performing assets (strong pumps) tend to mean-revert, especially when:
  - Broader market is under pressure (bearish macro)
  - Funding rates are strongly positive (longs over-extended, paying shorts)
  - Volume is declining while price is still near highs (distribution phase)
- In a bearish macro environment, this strategy is most active exactly when
  the Aegis long strategy is most dormant — genuine complementarity
- Futures shorts have a mechanical tailwind from positive funding rates
- The current Jun 2026 bearish market is a real-world example of what this
  strategy would have been watching

---

## Signal Definitions (locked in)

### Step 1 — Top Performer Detection (asset qualifies for watching)
An asset enters the watch list when ALL of:
- 24h price change > +8% (significant pump, not noise)
- Volume ratio > 2.0x vs 20-period 1H average (confirmed participation)
- Currently in top 3 gainers on the futures watchlist by 24h %

### Step 2 — Reversal Signal Detection (potential short entry)
A reversal signal fires when 2 or more of:
- RSI(1H) crosses below 70 (from overbought territory)
- Volume declining for 2 consecutive 1H candles while price still near high
  (distribution pattern — sellers absorbing buyers)
- Funding rate > +0.06% (longs over-extended, mechanical short tailwind)
- 1H candle closes below 20-period SMA after being above it
  (trend structure breaking)

### Step 3 — Outcome Tracking (what happens after the signal)
Record price at signal time, then log:
- 4H outcome: % move from signal price
- 24H outcome: % move from signal price
- 72H outcome: % move from signal price
- Did it reverse >3%? >5%? >10%?
- Which specific signals were present

---

## Learning File Format (aegis_reversal_observations.md)

Each observation entry:

```
## OBSERVATION #N — YYYY-MM-DD HH:MM UTC

Asset          : XXXUSDT
Entry price    : $X.XX (price at signal time)
24h gain       : +X.X% (what triggered watching)
Volume ratio   : X.Xx (at signal time)

Signals present:
  [ ] RSI(1H) crossed below 70
  [ ] Volume declining 2+ consecutive candles
  [ ] Funding rate > +0.06% (rate: X.XX%)
  [ ] 1H close below 20-period SMA
  Signals count: X/4

Market regime  : [Bearish/Sideways/Weak bull/Strong bull]
BTC regime     : [for context]

OUTCOME
  4H   : +X.X% / -X.X% (reversal: YES/NO)
  24H  : +X.X% / -X.X% (reversal: YES/NO)
  72H  : +X.X% / -X.X% (reversal: YES/NO)
  Max reversal : -X.X% (within 72H)

Notes: [any relevant context — news, broader market, anomalies]
---
```

After 30+ observations, compute:
- Win rate by signal count (1/4 vs 2/4 vs 3/4 vs 4/4 signals)
- Average reversal depth by timeframe
- Best performing signal combinations
- Regime dependency (does it work better in Bearish vs Sideways?)

---

## Implementation Plan (for Claude Code session)

### New file: aegis_reversal_watcher.py
- Standalone script, no shared state with aegis_bot.py or aegis_server.py
- Runs as a third process (separate terminal)
- Reads from Binance public API (klines, ticker, funding rates via fapi)
  Note: fapi may be geo-blocked — handle gracefully, log warning if so
- Scans every 15 minutes (same cadence as spot bot, but independent)
- Writes observations to aegis_reversal_observations.md
- Never touches aegis_server.py, aegis_bot.py, aegis.db, or signals.csv
- No orders, no server calls, pure read-only observation

### Futures watchlist (starting 5)
- BTCUSDT (baseline, most liquid)
- ETHUSDT (second most liquid, high volatility)
- SOLUSDT (high volatility, active perp market)
- BNBUSDT (strong correlation to market)
- XRPUSDT (high speculative interest, sharp pump/reversal cycles)

Intentional overlap with spot watchlist — compare same asset through both
lenses simultaneously. Spot bot sees BTC as a long candidate; reversal watcher
sees BTC as a short candidate when it pumps. Different logic, different files,
same asset.

### Architecture principle
```
Terminal 1: python aegis_server.py     ← unchanged
Terminal 2: python aegis_bot.py        ← unchanged
Terminal 3: python aegis_reversal_watcher.py  ← NEW, observation only
```

---

## Known Constraints

1. **Funding rate API may be geo-blocked** — fapi.binance.com is blocked from
   Vijay's ISP (same as Telegram). The watcher should detect this gracefully
   and still log observations without funding rate data, marking that field
   as "unavailable". A VPN would unlock this signal.

2. **No futures trading yet** — this module is observation-only. Futures
   testnet integration is a separate, later step. The watcher never places
   orders regardless of what it detects.

3. **Manual outcome logging** — the 4H/24H/72H outcomes need to be filled in
   after the fact (the script can log the entry, outcomes need a follow-up
   check). Consider a companion script that checks open observations and
   fills in outcomes automatically.

4. **Keep it simple** — the first version should be minimal: detect top
   performers, log reversal signals, write to the observations file. No
   dashboard, no scoring, no AI commentary. Add sophistication after the
   first 30 observations validate the approach.

---

## Relationship to Aegis

This is NOT part of Aegis. It is a parallel research project that runs
alongside Aegis without interfering with it. If this research eventually
produces a validated edge, the short engine would be built as a separate
system (not integrated into aegis_bot.py) and would go through its own
testing gate before any capital is committed.

The Aegis long system and the Reversal Short Engine are independent strategies
that happen to run on the same machine. They share no code, no state, no files.

---

## Version History

| Date | Note |
|---|---|
| 2026-06-25 | Initial design — strategy defined, signal definitions locked, architecture planned |
