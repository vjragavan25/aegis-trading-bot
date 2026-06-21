"""
Aegis Backtester
=================
Replays the EXACT scoring logic from aegis_bot.py against historical 
Binance data to measure whether the model has a statistical edge.

This answers: "If this bot had been running for the last N months,
what would the win rate, R:R, and drawdown have looked like?"

Run: python aegis_backtest.py

Output:
  - Console summary: win rate, avg R:R, max drawdown, accuracy by regime
  - aegis_backtest_trades.csv: every simulated trade
  - aegis_backtest_report.txt: full readable report

IMPORTANT: Uses api.binance.com (NOT testnet) for historical data,
since testnet only has recent data. This is read-only public data —
no API key needed.
"""

import json
import csv
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────────────────────────
# CONFIGURATION — mirrors aegis_bot.py exactly
# ─────────────────────────────────────────────────────────────────
REST_BASE = "https://api.binance.com/api/v3"

WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

# How far back to test (months)
BACKTEST_MONTHS = 12

# Entry thresholds — IDENTICAL to aegis_bot.py
ENTRY_THRESHOLDS = {
    "min_opp_score":    68,
    "min_conf_score":   65,
    "max_risk_score":   50,
    "min_volume_ratio": 1.2,
    "min_tf_bullish":   2,
}

# Risk management — IDENTICAL to aegis_bot.py
STOP_LOSS_PCT   = 0.025
TAKE_PROFIT_PCT = 0.055

# Simulated starting balance for drawdown calc
STARTING_BALANCE = 10000.0
RISK_PER_TRADE_PCT = 1.0

# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")

def fetch_klines(symbol, interval, start_ms, end_ms, limit=1000):
    """Fetch klines from Binance public API with pagination."""
    all_klines = []
    cur = start_ms
    while cur < end_ms:
        url = (f"{REST_BASE}/klines?symbol={symbol}&interval={interval}"
               f"&startTime={cur}&endTime={end_ms}&limit={limit}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
        except Exception as e:
            log(f"  Error fetching {symbol} {interval}: {e}")
            break
        if not data:
            break
        all_klines.extend(data)
        last_close_time = data[-1][6]  # close time of last candle
        if len(data) < limit:
            break
        cur = last_close_time + 1
        time.sleep(0.15)  # be polite to the API
    return all_klines

def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n

# ─────────────────────────────────────────────────────────────────
# SCORING LOGIC — copied exactly from aegis_bot.py calc_signals()
# ─────────────────────────────────────────────────────────────────
def score_at_index(c1h, v1h, c4h, cd, idx_1h, idx_4h, idx_d, pct_24h):
    """
    Computes the 5-factor score at a given point in history.
    idx_1h, idx_4h, idx_d are the indices into each candle array
    representing "now" for this simulated scan.
    """
    if idx_1h < 50 or idx_4h < 30 or idx_d < 20:
        return None  # not enough history yet

    last    = c1h[idx_1h]
    last4h  = c4h[idx_4h]
    lastd   = cd[idx_d]

    sma20_1h = sma(c1h[:idx_1h+1], 20)
    sma50_1h = sma(c1h[:idx_1h+1], 50)
    sma10_4h = sma(c4h[:idx_4h+1], 10)
    sma20_4h = sma(c4h[:idx_4h+1], 20)
    sma10_d  = sma(cd[:idx_d+1],  10)

    if not all([sma20_1h, sma50_1h, sma10_4h, sma20_4h, sma10_d]):
        return None

    avg_vol   = sma(v1h[:idx_1h+1], 20)
    last_vol  = v1h[idx_1h]
    vol_ratio = last_vol / avg_vol if avg_vol and avg_vol > 0 else 0

    tf_1h = last   > sma20_1h
    tf_4h = last4h > sma10_4h and last4h > sma20_4h
    tf_d  = lastd  > sma10_d
    tf_bullish = sum([tf_1h, tf_4h, tf_d])

    trend_score = 76 if (last > sma20_1h and sma20_1h > sma50_1h) else 58 if last > sma20_1h else 28
    mom_score   = (84 if pct_24h > 5 else 74 if pct_24h > 3 else 62 if pct_24h > 1 else
                   52 if pct_24h > 0 else 38 if pct_24h > -2 else 22)
    vol_score   = (88 if vol_ratio > 2 else 76 if vol_ratio > 1.5 else 64 if vol_ratio > 1.2 else
                   50 if vol_ratio > 0.9 else 36 if vol_ratio > 0.7 else 22)
    # Order book bias not available historically — use neutral 50
    ob_score    = 50
    tf_score    = 88 if tf_bullish == 3 else 65 if tf_bullish == 2 else 40 if tf_bullish == 1 else 20

    opp_score  = round(trend_score*0.25 + mom_score*0.22 + vol_score*0.18 + ob_score*0.15 + tf_score*0.20)
    conf_score = min(95, round(opp_score * 0.88 + tf_bullish * 4))
    risk_score = max(15, round(100 - opp_score * 0.68 + (12 if vol_ratio < 0.8 else 0)))

    regime = ("Strong bull" if last > sma20_1h and last > sma50_1h and pct_24h > 1 and tf_bullish == 3
              else "Weak bull" if last > sma20_1h and pct_24h > 0
              else "Bearish"   if last < sma20_1h and pct_24h < -1
              else "Sideways")

    return {
        "opp_score": opp_score, "conf_score": conf_score, "risk_score": risk_score,
        "volume_ratio": round(vol_ratio,2), "tf_bullish": tf_bullish, "regime": regime,
        "price": last
    }

def should_trade(scores):
    t = ENTRY_THRESHOLDS
    checks = {
        "opp_score":    scores["opp_score"]    >= t["min_opp_score"],
        "conf_score":   scores["conf_score"]   >= t["min_conf_score"],
        "risk_score":   scores["risk_score"]   <= t["max_risk_score"],
        "volume_ratio": scores["volume_ratio"] >= t["min_volume_ratio"],
        "tf_bullish":   scores["tf_bullish"]   >= t["min_tf_bullish"],
    }
    return all(checks.values())

# ─────────────────────────────────────────────────────────────────
# TRADE SIMULATION
# ─────────────────────────────────────────────────────────────────
def simulate_trade(c1h, idx_1h, entry_price, scores, symbol, entry_time):
    """
    Simulates a trade starting at idx_1h.
    Walks forward through 1H candles checking for SL or TP hit.
    Max hold: 7 days (168 hours) — if neither hit, close at market.
    """
    stop   = entry_price * (1 - STOP_LOSS_PCT)
    target = entry_price * (1 + TAKE_PROFIT_PCT)
    max_hold = 168  # hours

    for i in range(1, min(max_hold, len(c1h) - idx_1h)):
        future_idx = idx_1h + i
        if future_idx >= len(c1h):
            break
        price = c1h[future_idx]

        if price <= stop:
            pnl_pct = -STOP_LOSS_PCT * 100
            return {"outcome": "loss", "exit_price": stop, "exit_idx": future_idx,
                    "pnl_pct": pnl_pct, "rr": -1.0, "duration_h": i}
        if price >= target:
            pnl_pct = TAKE_PROFIT_PCT * 100
            rr = TAKE_PROFIT_PCT / STOP_LOSS_PCT
            return {"outcome": "win", "exit_price": target, "exit_idx": future_idx,
                    "pnl_pct": pnl_pct, "rr": rr, "duration_h": i}

    # Timed out — close at current price
    final_idx = min(idx_1h + max_hold, len(c1h) - 1)
    final_price = c1h[final_idx]
    pnl_pct = ((final_price - entry_price) / entry_price) * 100
    rr = pnl_pct / (STOP_LOSS_PCT * 100) if pnl_pct != 0 else 0
    outcome = "win" if pnl_pct > 0 else "loss" if pnl_pct < 0 else "flat"
    return {"outcome": outcome, "exit_price": final_price, "exit_idx": final_idx,
            "pnl_pct": pnl_pct, "rr": rr, "duration_h": max_hold}

# ─────────────────────────────────────────────────────────────────
# MAIN BACKTEST
# ─────────────────────────────────────────────────────────────────
def run_backtest():
    print("=" * 60)
    print("  Aegis Backtester")
    print("=" * 60)
    print(f"  Period   : Last {BACKTEST_MONTHS} months")
    print(f"  Assets   : {', '.join(WATCHLIST)}")
    print(f"  Entry gate: Opp>={ENTRY_THRESHOLDS['min_opp_score']} "
          f"Conf>={ENTRY_THRESHOLDS['min_conf_score']} "
          f"Risk<={ENTRY_THRESHOLDS['max_risk_score']} "
          f"Vol>={ENTRY_THRESHOLDS['min_volume_ratio']}x")
    print(f"  SL/TP    : {STOP_LOSS_PCT*100}% / {TAKE_PROFIT_PCT*100}% "
          f"({TAKE_PROFIT_PCT/STOP_LOSS_PCT:.1f}:1 R:R)")
    print("=" * 60)

    end_time   = int(time.time() * 1000)
    start_time = end_time - (BACKTEST_MONTHS * 30 * 24 * 60 * 60 * 1000)

    all_trades = []

    for symbol in WATCHLIST:
        log(f"Fetching historical data for {symbol}...")

        k1h = fetch_klines(symbol, "1h", start_time, end_time)
        time.sleep(0.5)
        k4h = fetch_klines(symbol, "4h", start_time, end_time)
        time.sleep(0.5)
        kd  = fetch_klines(symbol, "1d", start_time, end_time)
        time.sleep(0.5)

        if not k1h or not k4h or not kd:
            log(f"  Failed to fetch data for {symbol}, skipping")
            continue

        log(f"  Got {len(k1h)} x 1H, {len(k4h)} x 4H, {len(kd)} x Daily candles")

        c1h = [float(k[4]) for k in k1h]
        v1h = [float(k[5]) for k in k1h]
        c4h = [float(k[4]) for k in k4h]
        cd  = [float(k[4]) for k in kd]
        times_1h = [k[0] for k in k1h]

        # Walk through 1H candles, checking entry gate at each point
        in_trade_until = -1  # index until which we're "in" a trade (no overlapping trades)

        log(f"  Simulating {len(c1h)} hourly windows...")
        for idx_1h in range(50, len(c1h) - 1):
            if idx_1h <= in_trade_until:
                continue  # skip — already in a simulated trade

            # Map 1H index to corresponding 4H and Daily index
            current_time = times_1h[idx_1h]
            idx_4h = sum(1 for t in c4h if True) - 1  # placeholder
            # Find closest 4h candle index by time
            idx_4h = 0
            for j, k in enumerate(k4h):
                if k[0] <= current_time:
                    idx_4h = j
                else:
                    break
            idx_d = 0
            for j, k in enumerate(kd):
                if k[0] <= current_time:
                    idx_d = j
                else:
                    break

            # 24h pct change (compare current close to close 24h ago)
            if idx_1h >= 24:
                pct_24h = ((c1h[idx_1h] - c1h[idx_1h-24]) / c1h[idx_1h-24]) * 100
            else:
                pct_24h = 0

            scores = score_at_index(c1h, v1h, c4h, cd, idx_1h, idx_4h, idx_d, pct_24h)
            if not scores:
                continue

            if should_trade(scores):
                entry_price = c1h[idx_1h]
                entry_time  = datetime.fromtimestamp(times_1h[idx_1h]/1000, tz=timezone.utc)

                result = simulate_trade(c1h, idx_1h, entry_price, scores, symbol, entry_time)

                trade = {
                    "symbol":     symbol,
                    "entry_time": entry_time.strftime("%Y-%m-%d %H:%M"),
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(result["exit_price"], 4),
                    "outcome":    result["outcome"],
                    "pnl_pct":    round(result["pnl_pct"], 3),
                    "rr":         round(result["rr"], 2),
                    "duration_h": result["duration_h"],
                    "opp_score":  scores["opp_score"],
                    "conf_score": scores["conf_score"],
                    "risk_score": scores["risk_score"],
                    "volume_ratio": scores["volume_ratio"],
                    "tf_bullish": scores["tf_bullish"],
                    "regime":     scores["regime"],
                }
                all_trades.append(trade)

                # Mark as "in trade" to avoid overlapping signals
                in_trade_until = result["exit_idx"]

    return all_trades

# ─────────────────────────────────────────────────────────────────
# ANALYSIS & REPORT
# ─────────────────────────────────────────────────────────────────
def analyze_results(trades):
    if not trades:
        print("\n" + "="*60)
        print("  NO TRADES TRIGGERED IN BACKTEST PERIOD")
        print("="*60)
        print("\n  The entry gate never fired across the backtest period.")
        print("  This could mean:")
        print("  - Thresholds are too strict for historical conditions")
        print("  - Order book bias (ob_score=50 neutral) is masking real signals")
        print("  - Consider loosening thresholds and re-testing")
        return

    total = len(trades)
    wins  = [t for t in trades if t["outcome"] == "win"]
    losses = [t for t in trades if t["outcome"] == "loss"]
    flats = [t for t in trades if t["outcome"] == "flat"]

    win_rate = len(wins) / total * 100 if total else 0
    avg_rr_win = sum(t["rr"] for t in wins) / len(wins) if wins else 0
    avg_rr_loss = sum(t["rr"] for t in losses) / len(losses) if losses else 0
    avg_pnl = sum(t["pnl_pct"] for t in trades) / total

    # Equity curve & max drawdown (using R-multiples, 1% risk per trade)
    equity = STARTING_BALANCE
    peak = equity
    max_dd = 0
    equity_curve = [equity]
    for t in trades:
        risk_amount = equity * (RISK_PER_TRADE_PCT / 100)
        pnl = risk_amount * t["rr"]
        equity += pnl
        equity_curve.append(equity)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd:
            max_dd = dd

    total_return = (equity - STARTING_BALANCE) / STARTING_BALANCE * 100

    # By regime
    regime_stats = {}
    for t in trades:
        r = t["regime"]
        if r not in regime_stats:
            regime_stats[r] = {"total":0, "wins":0}
        regime_stats[r]["total"] += 1
        if t["outcome"] == "win":
            regime_stats[r]["wins"] += 1

    # By symbol
    symbol_stats = {}
    for t in trades:
        s = t["symbol"]
        if s not in symbol_stats:
            symbol_stats[s] = {"total":0, "wins":0}
        symbol_stats[s]["total"] += 1
        if t["outcome"] == "win":
            symbol_stats[s]["wins"] += 1

    # Build report
    lines = []
    lines.append("="*60)
    lines.append("  AEGIS BACKTEST REPORT")
    lines.append(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("="*60)
    lines.append("")
    lines.append(f"  Total simulated trades : {total}")
    lines.append(f"  Wins / Losses / Flat   : {len(wins)} / {len(losses)} / {len(flats)}")
    lines.append(f"  Win rate               : {win_rate:.1f}%")
    lines.append(f"  Avg R on wins          : {avg_rr_win:.2f}R")
    lines.append(f"  Avg R on losses        : {avg_rr_loss:.2f}R")
    lines.append(f"  Avg PnL per trade      : {avg_pnl:+.2f}%")
    lines.append(f"  Expectancy             : {(win_rate/100 * avg_rr_win + (1-win_rate/100) * avg_rr_loss):.3f}R per trade")
    lines.append("")
    lines.append(f"  Starting balance       : ${STARTING_BALANCE:,.2f}")
    lines.append(f"  Ending balance         : ${equity:,.2f}")
    lines.append(f"  Total return           : {total_return:+.1f}%")
    lines.append(f"  Max drawdown           : {max_dd:.1f}%")
    lines.append("")
    lines.append("-"*60)
    lines.append("  BY MARKET REGIME")
    lines.append("-"*60)
    for r, s in sorted(regime_stats.items(), key=lambda x: -x[1]["total"]):
        wr = s["wins"]/s["total"]*100 if s["total"] else 0
        lines.append(f"  {r:20s} {s['total']:4d} trades | {wr:5.1f}% win rate")
    lines.append("")
    lines.append("-"*60)
    lines.append("  BY ASSET")
    lines.append("-"*60)
    for s, st in sorted(symbol_stats.items(), key=lambda x: -x[1]["total"]):
        wr = st["wins"]/st["total"]*100 if st["total"] else 0
        lines.append(f"  {s:20s} {st['total']:4d} trades | {wr:5.1f}% win rate")
    lines.append("")
    lines.append("="*60)
    lines.append("  VERDICT")
    lines.append("="*60)

    expectancy = win_rate/100 * avg_rr_win + (1-win_rate/100) * avg_rr_loss
    if total < 30:
        lines.append(f"  ⚠ Only {total} trades — sample size too small for confidence.")
        lines.append("  Consider extending backtest period or loosening thresholds.")
    elif expectancy > 0.15 and win_rate >= 50:
        lines.append(f"  ✓ POSITIVE EDGE DETECTED")
        lines.append(f"  Expectancy of {expectancy:.3f}R per trade with {win_rate:.1f}% win rate")
        lines.append(f"  This model shows statistical promise. Proceed with live demo testing.")
    elif expectancy > 0:
        lines.append(f"  ~ MARGINAL EDGE")
        lines.append(f"  Expectancy of {expectancy:.3f}R is positive but thin.")
        lines.append(f"  Consider refining entry conditions before relying on this model.")
    else:
        lines.append(f"  ✗ NO EDGE DETECTED")
        lines.append(f"  Expectancy of {expectancy:.3f}R is negative or zero.")
        lines.append(f"  This model would have LOST money historically.")
        lines.append(f"  DO NOT proceed to live trading. Review and adjust the model.")

    lines.append("="*60)

    report = "\n".join(lines)
    print("\n" + report)

    # Save report
    with open("aegis_backtest_report.txt", "w", encoding="utf-8") as f:
        f.write(report)

    # Save trades CSV
    if trades:
        with open("aegis_backtest_trades.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            writer.writeheader()
            writer.writerows(trades)

    print(f"\n  Saved: aegis_backtest_report.txt")
    print(f"  Saved: aegis_backtest_trades.csv ({len(trades)} trades)")

# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    trades = run_backtest()
    analyze_results(trades)
