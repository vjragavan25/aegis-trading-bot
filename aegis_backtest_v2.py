"""
Aegis Backtester — Run #2
==========================
Extends Run #1 with:
  - 24-month lookback (Jun 2024 → Jun 2026) for regime diversity
  - New entry filters: regime_quality (Strong bull only) +
    regime_consistency (prior 1H scan must also be Strong bull)
  - Per-regime, per-asset, per-month breakdown
  - Consecutive loss streak analysis
  - Monthly equity curve
  - Direct comparison table vs Run #1 results
  - Graceful fallback: if api.binance.com returns 403 (geo-blocked),
    falls back to demo-api.binance.com for recent data with a warning

Run: python aegis_backtest_v2.py

Output:
  - Console summary
  - aegis_backtest_v2_report.txt
  - aegis_backtest_v2_trades.csv
"""

import json
import csv
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ─────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────
# Primary: public Binance API (no key needed for klines)
# Fallback: demo API (recent data only, ~6 months)
DATA_SOURCES = [
    "https://api.binance.com/api/v3",
    "https://demo-api.binance.com/api/v3",
]

WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

BACKTEST_MONTHS = 24   # Run #2: 24 months vs Run #1's 12

# Entry thresholds — matches deployed aegis_bot.py
ENTRY_THRESHOLDS = {
    "min_opp_score":    68,
    "min_conf_score":   65,
    "max_risk_score":   50,
    "min_volume_ratio": 1.2,
    "min_tf_bullish":   2,
}

STOP_LOSS_PCT   = 0.025   # 2.5%
TAKE_PROFIT_PCT = 0.055   # 5.5%
MAX_HOLD_HOURS  = 168     # 7 days

STARTING_BALANCE   = 10000.0
RISK_PER_TRADE_PCT = 1.0

# Run #1 reference numbers for comparison table
RUN1 = {
    "trades": 148, "win_rate": 41.2, "expectancy": 0.266,
    "max_dd": 8.4, "total_return": 45.6,
    "months": 12, "period": "Jul 2025 to Jun 2026"
}

# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    try:
        print(f"[{ts}] {msg}")
    except UnicodeEncodeError:
        safe = f"[{ts}] {msg}".encode(
            sys.stdout.encoding or "utf-8", errors="replace"
        ).decode(sys.stdout.encoding or "utf-8", errors="replace")
        print(safe)


def fetch_klines(symbol, interval, start_ms, end_ms, limit=1000):
    """
    Fetch klines with pagination. Tries each REST_BASE in DATA_SOURCES
    in order, falls back on 403. Returns (klines, source_used).
    """
    rest_base = None
    for candidate in DATA_SOURCES:
        test_url = (f"{candidate}/klines?symbol={symbol}&interval={interval}"
                    f"&startTime={start_ms}&endTime={end_ms}&limit=1")
        try:
            req = urllib.request.Request(test_url,
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
            rest_base = candidate
            break
        except urllib.error.HTTPError as e:
            if e.code == 403:
                continue
            raise
        except Exception:
            continue

    if not rest_base:
        log(f"  All data sources blocked for {symbol} {interval}")
        return [], "none"

    all_klines = []
    cur = start_ms
    while cur < end_ms:
        url = (f"{rest_base}/klines?symbol={symbol}&interval={interval}"
               f"&startTime={cur}&endTime={end_ms}&limit={limit}")
        try:
            req = urllib.request.Request(url,
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
        except Exception as e:
            log(f"  Fetch error {symbol} {interval}: {e}")
            break
        if not data:
            break
        all_klines.extend(data)
        if len(data) < limit:
            break
        cur = data[-1][6] + 1  # next page starts after last candle close time
        time.sleep(0.15)

    return all_klines, rest_base


def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


# ─────────────────────────────────────────────────────────────────
# SCORING — identical to aegis_bot.py calc_signals()
# ─────────────────────────────────────────────────────────────────
def score_at_index(c1h, v1h, c4h, cd, idx_1h, idx_4h, idx_d, pct_24h):
    if idx_1h < 50 or idx_4h < 30 or idx_d < 20:
        return None

    last   = c1h[idx_1h]
    last4h = c4h[idx_4h]
    lastd  = cd[idx_d]

    sma20_1h = sma(c1h[:idx_1h+1], 20)
    sma50_1h = sma(c1h[:idx_1h+1], 50)
    sma10_4h = sma(c4h[:idx_4h+1], 10)
    sma20_4h = sma(c4h[:idx_4h+1], 20)
    sma10_d  = sma(cd[:idx_d+1],   10)

    if not all([sma20_1h, sma50_1h, sma10_4h, sma20_4h, sma10_d]):
        return None

    avg_vol   = sma(v1h[:idx_1h+1], 20)
    last_vol  = v1h[idx_1h]
    vol_ratio = (last_vol / avg_vol) if avg_vol and avg_vol > 0 else 0

    tf_1h = last   > sma20_1h
    tf_4h = last4h > sma10_4h and last4h > sma20_4h
    tf_d  = lastd  > sma10_d
    tf_bullish = sum([tf_1h, tf_4h, tf_d])

    trend_score = (76 if (last > sma20_1h and sma20_1h > sma50_1h)
                   else 58 if last > sma20_1h else 28)
    mom_score   = (84 if pct_24h > 5 else 74 if pct_24h > 3 else
                   62 if pct_24h > 1 else 52 if pct_24h > 0 else
                   38 if pct_24h > -2 else 22)
    vol_score   = (88 if vol_ratio > 2 else 76 if vol_ratio > 1.5 else
                   64 if vol_ratio > 1.2 else 50 if vol_ratio > 0.9 else
                   36 if vol_ratio > 0.7 else 22)
    ob_score    = 50  # order book not available historically
    tf_score    = (88 if tf_bullish == 3 else 65 if tf_bullish == 2
                   else 40 if tf_bullish == 1 else 20)

    opp_score  = round(trend_score*0.25 + mom_score*0.22 + vol_score*0.18
                       + ob_score*0.15 + tf_score*0.20)
    conf_score = min(95, round(opp_score * 0.88 + tf_bullish * 4))
    risk_score = max(15, round(100 - opp_score * 0.68
                               + (12 if vol_ratio < 0.8 else 0)))

    regime = ("Strong bull" if (last > sma20_1h and last > sma50_1h
                                and pct_24h > 1 and tf_bullish == 3)
              else "Weak bull" if (last > sma20_1h and pct_24h > 0)
              else "Bearish"   if (last < sma20_1h and pct_24h < -1)
              else "Sideways")

    return {
        "opp_score": opp_score, "conf_score": conf_score,
        "risk_score": risk_score, "volume_ratio": round(vol_ratio, 2),
        "tf_bullish": tf_bullish, "regime": regime, "price": last
    }


def should_trade(scores, prev_regime):
    """
    Run #2 entry gate — adds two regime filters vs Run #1:
      regime_quality:    current regime must be Strong bull
      regime_consistency: previous scan must also be Strong bull
                          (prev_regime=None means first scan → block)
    """
    t = ENTRY_THRESHOLDS
    regime_ok = (
        scores["regime"] == "Strong bull" and
        prev_regime == "Strong bull"
    )
    return (
        scores["opp_score"]    >= t["min_opp_score"]  and
        scores["conf_score"]   >= t["min_conf_score"]  and
        scores["risk_score"]   <= t["max_risk_score"]  and
        scores["volume_ratio"] >= t["min_volume_ratio"] and
        scores["tf_bullish"]   >= t["min_tf_bullish"]  and
        regime_ok
    )


# ─────────────────────────────────────────────────────────────────
# TRADE SIMULATION
# ─────────────────────────────────────────────────────────────────
def simulate_trade(c1h, idx_1h, entry_price):
    stop   = entry_price * (1 - STOP_LOSS_PCT)
    target = entry_price * (1 + TAKE_PROFIT_PCT)

    for i in range(1, min(MAX_HOLD_HOURS, len(c1h) - idx_1h)):
        future_idx = idx_1h + i
        if future_idx >= len(c1h):
            break
        price = c1h[future_idx]

        if price <= stop:
            return {"outcome": "loss", "exit_price": stop,
                    "exit_idx": future_idx, "pnl_pct": -STOP_LOSS_PCT * 100,
                    "rr": -1.0, "duration_h": i}
        if price >= target:
            rr = TAKE_PROFIT_PCT / STOP_LOSS_PCT
            return {"outcome": "win", "exit_price": target,
                    "exit_idx": future_idx, "pnl_pct": TAKE_PROFIT_PCT * 100,
                    "rr": rr, "duration_h": i}

    # Timed out
    final_idx   = min(idx_1h + MAX_HOLD_HOURS, len(c1h) - 1)
    final_price = c1h[final_idx]
    pnl_pct     = (final_price - entry_price) / entry_price * 100
    rr          = pnl_pct / (STOP_LOSS_PCT * 100) if pnl_pct != 0 else 0
    outcome     = "win" if pnl_pct > 0 else "loss" if pnl_pct < 0 else "flat"
    return {"outcome": outcome, "exit_price": final_price,
            "exit_idx": final_idx, "pnl_pct": pnl_pct,
            "rr": rr, "duration_h": MAX_HOLD_HOURS}


# ─────────────────────────────────────────────────────────────────
# MAIN BACKTEST
# ─────────────────────────────────────────────────────────────────
def run_backtest():
    print("=" * 60)
    print("  Aegis Backtester — Run #2")
    print("=" * 60)
    print(f"  Period    : Last {BACKTEST_MONTHS} months")
    print(f"  Assets    : {', '.join(WATCHLIST)}")
    print(f"  Gate      : Opp>={ENTRY_THRESHOLDS['min_opp_score']} "
          f"Conf>={ENTRY_THRESHOLDS['min_conf_score']} "
          f"Risk<={ENTRY_THRESHOLDS['max_risk_score']} "
          f"Vol>={ENTRY_THRESHOLDS['min_volume_ratio']}x "
          f"+ Strong bull x2 cycles")
    print(f"  SL/TP     : {STOP_LOSS_PCT*100:.1f}% / {TAKE_PROFIT_PCT*100:.1f}% "
          f"({TAKE_PROFIT_PCT/STOP_LOSS_PCT:.1f}:1 R:R)")
    print("=" * 60)

    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - (BACKTEST_MONTHS * 30 * 24 * 60 * 60 * 1000)

    all_trades  = []
    data_source = "unknown"

    for symbol in WATCHLIST:
        log(f"Fetching {symbol}...")

        k1h, src = fetch_klines(symbol, "1h",  start_ms, end_ms)
        time.sleep(0.5)
        k4h, _   = fetch_klines(symbol, "4h",  start_ms, end_ms)
        time.sleep(0.5)
        kd,  _   = fetch_klines(symbol, "1d",  start_ms, end_ms)
        time.sleep(0.5)

        if not k1h or not k4h or not kd:
            log(f"  No data for {symbol} — skipping")
            continue

        if src != "unknown":
            data_source = src

        log(f"  {symbol}: {len(k1h)} x 1H | {len(k4h)} x 4H | {len(kd)} x 1D "
            f"(source: {src.split('//')[1].split('/')[0]})")

        c1h      = [float(k[4]) for k in k1h]
        v1h      = [float(k[5]) for k in k1h]
        c4h      = [float(k[4]) for k in k4h]
        cd       = [float(k[4]) for k in kd]
        times_1h = [k[0] for k in k1h]

        in_trade_until = -1
        prev_regime    = None   # tracks regime_consistency

        log(f"  Simulating {len(c1h)} hourly windows...")
        for idx_1h in range(50, len(c1h) - 1):

            # Map to 4H and Daily indices by timestamp
            current_time = times_1h[idx_1h]
            idx_4h = max((j for j, k in enumerate(k4h)
                          if k[0] <= current_time), default=0)
            idx_d  = max((j for j, k in enumerate(kd)
                          if k[0] <= current_time), default=0)

            pct_24h = (((c1h[idx_1h] - c1h[idx_1h - 24]) /
                         c1h[idx_1h - 24]) * 100
                       if idx_1h >= 24 else 0)

            scores = score_at_index(c1h, v1h, c4h, cd,
                                    idx_1h, idx_4h, idx_d, pct_24h)
            if not scores:
                prev_regime = None
                continue

            current_regime = scores["regime"]

            if idx_1h > in_trade_until and should_trade(scores, prev_regime):
                entry_price = c1h[idx_1h]
                entry_time  = datetime.fromtimestamp(
                    times_1h[idx_1h] / 1000, tz=timezone.utc)

                result = simulate_trade(c1h, idx_1h, entry_price)

                all_trades.append({
                    "symbol":       symbol,
                    "entry_time":   entry_time.strftime("%Y-%m-%d %H:%M"),
                    "entry_price":  round(entry_price, 4),
                    "exit_price":   round(result["exit_price"], 4),
                    "outcome":      result["outcome"],
                    "pnl_pct":      round(result["pnl_pct"], 3),
                    "rr":           round(result["rr"], 2),
                    "duration_h":   result["duration_h"],
                    "opp_score":    scores["opp_score"],
                    "conf_score":   scores["conf_score"],
                    "risk_score":   scores["risk_score"],
                    "volume_ratio": scores["volume_ratio"],
                    "tf_bullish":   scores["tf_bullish"],
                    "regime":       current_regime,
                    "prev_regime":  prev_regime or "N/A",
                    "month":        entry_time.strftime("%Y-%m"),
                })
                in_trade_until = result["exit_idx"]

            prev_regime = current_regime

        log(f"  {symbol}: {sum(1 for t in all_trades if t['symbol']==symbol)} trades found")

    return all_trades, data_source


# ─────────────────────────────────────────────────────────────────
# ANALYSIS & REPORT
# ─────────────────────────────────────────────────────────────────
def analyze_results(trades, data_source):
    if not trades:
        print("\nNO TRADES TRIGGERED — check data availability and thresholds.")
        return

    total  = len(trades)
    wins   = [t for t in trades if t["outcome"] == "win"]
    losses = [t for t in trades if t["outcome"] == "loss"]
    flats  = [t for t in trades if t["outcome"] == "flat"]

    win_rate    = len(wins) / total * 100
    avg_rr_win  = sum(t["rr"] for t in wins)  / len(wins)  if wins  else 0
    avg_rr_loss = sum(t["rr"] for t in losses) / len(losses) if losses else 0
    expectancy  = win_rate/100 * avg_rr_win + (1 - win_rate/100) * avg_rr_loss
    avg_pnl     = sum(t["pnl_pct"] for t in trades) / total

    # Equity curve
    equity = STARTING_BALANCE
    peak   = equity
    max_dd = 0
    equity_curve = [equity]
    for t in trades:
        risk  = equity * (RISK_PER_TRADE_PCT / 100)
        equity += risk * t["rr"]
        equity_curve.append(equity)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd:
            max_dd = dd
    total_return = (equity - STARTING_BALANCE) / STARTING_BALANCE * 100

    # Consecutive loss streaks
    max_streak = cur_streak = 0
    for t in trades:
        if t["outcome"] == "loss":
            cur_streak += 1
            max_streak = max(max_streak, cur_streak)
        else:
            cur_streak = 0

    # By regime
    regime_stats = defaultdict(lambda: {"total": 0, "wins": 0,
                                         "pnl": 0.0, "rr": 0.0})
    for t in trades:
        r = t["regime"]
        regime_stats[r]["total"] += 1
        regime_stats[r]["pnl"]   += t["pnl_pct"]
        regime_stats[r]["rr"]    += t["rr"]
        if t["outcome"] == "win":
            regime_stats[r]["wins"] += 1

    # By asset
    asset_stats = defaultdict(lambda: {"total": 0, "wins": 0, "rr": 0.0})
    for t in trades:
        s = t["symbol"]
        asset_stats[s]["total"] += 1
        asset_stats[s]["rr"]    += t["rr"]
        if t["outcome"] == "win":
            asset_stats[s]["wins"] += 1

    # By month
    monthly = defaultdict(lambda: {"total": 0, "wins": 0, "rr": 0.0})
    for t in trades:
        m = t["month"]
        monthly[m]["total"] += 1
        monthly[m]["rr"]    += t["rr"]
        if t["outcome"] == "win":
            monthly[m]["wins"] += 1

    # Date range from actual data
    dates = sorted(t["entry_time"] for t in trades)
    period_str = f"{dates[0][:7]} to {dates[-1][:7]}" if dates else "N/A"

    # ── Build report ──
    L = []  # lines

    def h(text=""):    L.append(text)
    def div():         L.append("-" * 60)
    def bar():         L.append("=" * 60)

    bar()
    h("  AEGIS BACKTEST REPORT — RUN #2")
    h(f"  Generated : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    h(f"  Period    : {period_str}  ({BACKTEST_MONTHS}mo requested)")
    h(f"  Data src  : {data_source}")
    h(f"  New vs R1 : +regime_quality +regime_consistency filters")
    bar()
    h()
    h(f"  Total simulated trades : {total}")
    h(f"  Wins / Losses / Flat   : {len(wins)} / {len(losses)} / {len(flats)}")
    h(f"  Win rate               : {win_rate:.1f}%")
    h(f"  Avg R on wins          : {avg_rr_win:.2f}R")
    h(f"  Avg R on losses        : {avg_rr_loss:.2f}R")
    h(f"  Avg PnL per trade      : {avg_pnl:+.2f}%")
    h(f"  Expectancy             : {expectancy:.3f}R per trade")
    h()
    h(f"  Starting balance       : ${STARTING_BALANCE:,.2f}")
    h(f"  Ending balance         : ${equity:,.2f}")
    h(f"  Total return           : {total_return:+.1f}%")
    h(f"  Max drawdown           : {max_dd:.1f}%")
    h(f"  Max consecutive losses : {max_streak}")
    h()

    div()
    h("  RUN #2 vs RUN #1 COMPARISON")
    div()
    h(f"  {'Metric':<28} {'Run #1':>12} {'Run #2':>12}")
    h(f"  {'─'*28} {'─'*12} {'─'*12}")
    h(f"  {'Period':<28} {RUN1['period']:>12} {period_str:>12}")
    h(f"  {'Total trades':<28} {RUN1['trades']:>12} {total:>12}")
    h(f"  {'Win rate':<28} {RUN1['win_rate']:>11.1f}% {win_rate:>11.1f}%")
    h(f"  {'Expectancy (R)':<28} {RUN1['expectancy']:>12.3f} {expectancy:>12.3f}")
    h(f"  {'Max drawdown':<28} {RUN1['max_dd']:>11.1f}% {max_dd:>11.1f}%")
    h(f"  {'Total return':<28} {RUN1['total_return']:>11.1f}% {total_return:>11.1f}%")

    # Regime concentration comparison
    sb_count = regime_stats.get("Strong bull", {}).get("total", 0)
    sb_pct   = sb_count / total * 100 if total else 0
    h(f"  {'Strong bull %':<28} {'95.9%':>12} {sb_pct:>11.1f}%")
    h()

    div()
    h("  BY MARKET REGIME")
    div()
    for r, s in sorted(regime_stats.items(), key=lambda x: -x[1]["total"]):
        wr  = s["wins"] / s["total"] * 100 if s["total"] else 0
        avg = s["rr"] / s["total"] if s["total"] else 0
        pct = s["total"] / total * 100
        h(f"  {r:<20} {s['total']:4d} trades ({pct:4.1f}%) | "
          f"{wr:5.1f}% WR | {avg:+.3f}R avg")
    h()

    div()
    h("  BY ASSET")
    div()
    for s, st in sorted(asset_stats.items(), key=lambda x: -x[1]["total"]):
        wr  = st["wins"] / st["total"] * 100 if st["total"] else 0
        avg = st["rr"] / st["total"] if st["total"] else 0
        h(f"  {s:<20} {st['total']:4d} trades | {wr:5.1f}% WR | {avg:+.3f}R avg")
    h()

    div()
    h("  MONTHLY BREAKDOWN")
    div()
    h(f"  {'Month':<10} {'Trades':>7} {'WR%':>7} {'Avg R':>8} {'Cumul R':>9}")
    h(f"  {'─'*10} {'─'*7} {'─'*7} {'─'*8} {'─'*9}")
    cumul_r = 0.0
    for m in sorted(monthly.keys()):
        d  = monthly[m]
        wr = d["wins"] / d["total"] * 100 if d["total"] else 0
        ar = d["rr"] / d["total"] if d["total"] else 0
        cumul_r += d["rr"]
        h(f"  {m:<10} {d['total']:>7} {wr:>6.1f}% {ar:>+8.3f} {cumul_r:>+9.3f}")
    h()

    div()
    h("  VERDICT")
    div()
    if total < 30:
        h(f"  !! Only {total} trades — sample size too small.")
        h(f"     Extend period or check data availability.")
    elif expectancy > 0.2 and win_rate >= 45:
        h(f"  POSITIVE EDGE DETECTED")
        h(f"  Expectancy {expectancy:.3f}R | Win rate {win_rate:.1f}%")
        h(f"  Both regime filters confirmed — results are more reliable than R1.")
    elif expectancy > 0:
        h(f"  MARGINAL EDGE")
        h(f"  Expectancy {expectancy:.3f}R is positive but thin.")
        if sb_pct > 90:
            h(f"  WARNING: {sb_pct:.0f}% of trades are in Strong bull regime.")
            h(f"  Edge may not survive bear/ranging markets.")
            h(f"  Check data availability — 24mo should show more regime diversity.")
    else:
        h(f"  NO EDGE DETECTED")
        h(f"  Expectancy {expectancy:.3f}R — this model loses money historically.")
        h(f"  DO NOT proceed to live trading.")

    if data_source and "demo" in data_source:
        h()
        h(f"  !! DATA WARNING: Used demo-api fallback (recent data only).")
        h(f"     api.binance.com appears geo-blocked from this network.")
        h(f"     Results may not cover the full {BACKTEST_MONTHS}-month period.")
        h(f"     For full 24mo coverage: use a VPN and re-run.")

    bar()

    report = "\n".join(L)

    # Print encoding-safe (handles Windows CP1252 terminals)
    for line in L:
        try:
            print(line)
        except UnicodeEncodeError:
            safe = line.encode(
                sys.stdout.encoding or "utf-8", errors="replace"
            ).decode(sys.stdout.encoding or "utf-8", errors="replace")
            print(safe)

    # Save report (always UTF-8 regardless of terminal encoding)
    with open("aegis_backtest_v2_report.txt", "w", encoding="utf-8") as f:
        f.write(report)

    # Save trades CSV
    if trades:
        with open("aegis_backtest_v2_trades.csv", "w",
                  newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            writer.writeheader()
            writer.writerows(trades)

    print(f"\n  Saved: aegis_backtest_v2_report.txt")
    print(f"  Saved: aegis_backtest_v2_trades.csv ({total} trades)")


# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        trades, source = run_backtest()
        analyze_results(trades, source)
    except Exception as e:
        import traceback
        print("\n!! BACKTEST CRASHED:")
        traceback.print_exc()
        print(f"\nError: {e}")
        print("Please upload this error output for diagnosis.")
