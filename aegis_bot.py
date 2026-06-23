"""
Aegis Automated Trading Bot
============================
The brain of the automated system.
Runs the monitor → analyse → decide → execute cycle continuously.

Requires: aegis_server.py running in another terminal first.

Start: python aegis_bot.py
Stop:  Ctrl+C
"""

import json
import time
import os
import aegis_ai
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
BINANCE_REST    = "https://demo-api.binance.com/api/v3"
AEGIS_SERVER    = "http://localhost:8888"
SCAN_INTERVAL   = 60 * 15
WATCHLIST       = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT"]

# ─────────────────────────────────────────────
# LOG FILE — written alongside this script
# Rotates daily: aegis_bot_YYYY-MM-DD.log
# Keeps last 7 days automatically.
# ─────────────────────────────────────────────
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
LOG_KEEP_DAYS = 7

def _bot_log_path():
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(SCRIPT_DIR, f"aegis_bot_{date_str}.log")

def _rotate_old_logs():
    """Delete bot log files older than LOG_KEEP_DAYS."""
    try:
        for fname in os.listdir(SCRIPT_DIR):
            if fname.startswith("aegis_bot_") and fname.endswith(".log"):
                fpath = os.path.join(SCRIPT_DIR, fname)
                age_days = (datetime.now().timestamp() - os.path.getmtime(fpath)) / 86400
                if age_days > LOG_KEEP_DAYS:
                    os.remove(fpath)
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────
# MODULE-LEVEL STATE
# ─────────────────────────────────────────────────────────────────
_last_morning_brief_date = None
_cycle_count_total       = 0
_last_regime             = {}   # symbol -> regime string from previous scan cycle
                                 # used by should_trade() for regime consistency check

# Entry thresholds — all must pass for a trade to fire
ENTRY_THRESHOLDS = {
    "min_opp_score":    68,
    "min_conf_score":   65,
    "max_risk_score":   50,
    "min_volume_ratio": 1.2,
    "min_tf_bullish":   2,    # out of 3 timeframes
}

# Risk management
STOP_LOSS_PCT   = 0.025   # 2.5% below entry
TAKE_PROFIT_PCT = 0.055   # 5.5% above entry  → 2.2:1 R:R

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def log(msg, level="INFO"):
    ts  = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%H:%M:%S")
    sym = {"INFO":"›","OK":"✓","WARN":"⚠","ERR":"✗","TRADE":"◈","SCAN":"⊛"}.get(level,"›")
    line = f"[{ts}] {sym} {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        import sys
        print(line.encode(sys.stdout.encoding or "utf-8", errors="replace")
                  .decode(sys.stdout.encoding or "utf-8", errors="replace"))
    # Write to daily log file
    try:
        ts_full = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        with open(_bot_log_path(), "a", encoding="utf-8") as f:
            f.write(f"[{ts_full}] [{level}] {msg}\n")
    except Exception:
        pass  # Never let file I/O break the bot

def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"Fetch error {url}: {e}", "ERR")
        return None

def post_server(endpoint, data):
    try:
        body = json.dumps(data).encode()
        req  = urllib.request.Request(
            AEGIS_SERVER + endpoint, data=body,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"Server POST error: {e}", "ERR")
        return None

# ─────────────────────────────────────────────
# MARKET DATA
# ─────────────────────────────────────────────
def fetch_ticker(symbol):
    return fetch(f"{BINANCE_REST}/ticker/24hr?symbol={symbol}")

def fetch_klines(symbol, interval, limit=50):
    return fetch(f"{BINANCE_REST}/klines?symbol={symbol}&interval={interval}&limit={limit}")

def fetch_depth(symbol, limit=20):
    return fetch(f"{BINANCE_REST}/depth?symbol={symbol}&limit={limit}")

# ─────────────────────────────────────────────
# INDICATOR CALCULATIONS
# ─────────────────────────────────────────────
def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n

def calc_signals(symbol):
    log(f"Analysing {symbol}...", "SCAN")

    ticker = fetch_ticker(symbol)
    k1h    = fetch_klines(symbol, "1h", 50)
    k4h    = fetch_klines(symbol, "4h", 30)
    kd     = fetch_klines(symbol, "1d", 55)   # 55 so SMA50 has enough history
    depth  = fetch_depth(symbol)

    if not all([ticker, k1h, k4h, kd, depth]):
        log(f"Data fetch incomplete for {symbol}", "ERR")
        return None

    # CRITICAL SAFETY CHECK: verify the ticker response actually matches
    # the symbol we requested. If Binance returns data for a different
    # symbol (seen once with SOLUSDT returning BTC pricing), reject this
    # scan entirely rather than using corrupted price data.
    returned_symbol = ticker.get("symbol")
    if returned_symbol != symbol:
        log(f"SYMBOL MISMATCH: requested {symbol} but ticker returned "
            f"data for '{returned_symbol}'. Rejecting this scan.", "ERR")
        return None

    # Parse closes and volumes
    c1h = [float(k[4]) for k in k1h]
    c4h = [float(k[4]) for k in k4h]
    cd  = [float(k[4]) for k in kd]
    v1h = [float(k[5]) for k in k1h]

    last    = c1h[-1]
    last4h  = c4h[-1]
    lastd   = cd[-1]
    pct     = float(ticker["priceChangePercent"])
    price   = float(ticker["lastPrice"])

    # SAFETY CHECK 2: ticker price and 1H kline close should be within
    # a sane range of each other (a few % at most). If they differ
    # wildly, the data sources disagree -- reject this scan.
    if last > 0:
        price_diff_pct = abs(price - last) / last * 100
        if price_diff_pct > 5:
            log(f"PRICE MISMATCH for {symbol}: ticker price ${price} vs "
                f"1H candle close ${last} differ by {price_diff_pct:.1f}%. "
                f"Rejecting this scan.", "ERR")
            return None

    # Moving averages
    sma20_1h = sma(c1h, 20)
    sma50_1h = sma(c1h, 50)
    sma10_4h = sma(c4h, 10)
    sma20_4h = sma(c4h, 20)
    sma10_d  = sma(cd,  10)
    sma50_d  = sma(cd,  50)   # Daily SMA50 — primary trend filter

    if not all([sma20_1h, sma50_1h, sma10_4h, sma20_4h, sma10_d]):
        return None

    if not all([sma20_1h, sma50_1h, sma10_4h, sma20_4h, sma10_d]):
        return None

    # daily_trend_ok: current 1D close must be above the 50-period daily SMA.
    # Backtest finding: Feb 2025 had 0% win rate across 6 entries despite
    # passing all other filters — price was below the daily 50 SMA during
    # a correction. This single check would have blocked most of those entries.
    # If sma50_d is unavailable (fewer than 50 daily candles in history),
    # default to True so the filter doesn't silently block legitimate signals
    # during the first 50 trading days of a new asset being added.
    daily_trend_ok = (lastd > sma50_d) if sma50_d is not None else True

    # Volume ratio
    avg_vol    = sma(v1h, 20)
    last_vol   = v1h[-1]
    vol_ratio  = last_vol / avg_vol if avg_vol > 0 else 0

    # Order book bias
    bid_notional = sum(float(p)*float(q) for p,q in depth["bids"][:10])
    ask_notional = sum(float(p)*float(q) for p,q in depth["asks"][:10])
    ob_bias      = bid_notional / (bid_notional + ask_notional) if (bid_notional + ask_notional) > 0 else 0.5

    # Timeframe alignment
    tf_1h = last   > sma20_1h
    tf_4h = last4h > sma10_4h and last4h > sma20_4h
    tf_d  = lastd  > sma10_d
    tf_bullish = sum([tf_1h, tf_4h, tf_d])

    # Factor scores
    trend_score = 76 if (last > sma20_1h and sma20_1h > sma50_1h) else 58 if last > sma20_1h else 28
    mom_score   = (84 if pct > 5 else 74 if pct > 3 else 62 if pct > 1 else
                   52 if pct > 0 else 38 if pct > -2 else 22)
    vol_score   = (88 if vol_ratio > 2 else 76 if vol_ratio > 1.5 else 64 if vol_ratio > 1.2 else
                   50 if vol_ratio > 0.9 else 36 if vol_ratio > 0.7 else 22)
    ob_score    = round(ob_bias * 100)
    tf_score    = 88 if tf_bullish == 3 else 65 if tf_bullish == 2 else 40 if tf_bullish == 1 else 20

    opp_score  = round(trend_score*0.25 + mom_score*0.22 + vol_score*0.18 + ob_score*0.15 + tf_score*0.20)
    conf_score = min(95, round(opp_score * 0.88 + tf_bullish * 4))
    risk_score = max(15, round(100 - opp_score * 0.68 + (12 if vol_ratio < 0.8 else 0)))

    regime = ("Strong bull" if last > sma20_1h and last > sma50_1h and pct > 1 and tf_bullish == 3
              else "Weak bull" if last > sma20_1h and pct > 0
              else "Bearish"   if last < sma20_1h and pct < -1
              else "Sideways")

    return {
        "symbol":         symbol,
        "price":          price,
        "pct_24h":        round(pct, 2),
        "opp_score":      opp_score,
        "conf_score":     conf_score,
        "risk_score":     risk_score,
        "volume_ratio":   round(vol_ratio, 2),
        "ob_bias":        round(ob_bias, 3),
        "tf_bullish":     tf_bullish,
        "tf_1h":          tf_1h,
        "tf_4h":          tf_4h,
        "tf_d":           tf_d,
        "regime":         regime,
        "sma20_1h":       round(sma20_1h, 2),
        "sma50_1h":       round(sma50_1h, 2),
        "sma20_4h":       round(sma20_4h, 2),
        "sma50_d":        round(sma50_d, 2) if sma50_d else None,
        "daily_trend_ok": daily_trend_ok,
        "scanned_at":     datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    }

# ─────────────────────────────────────────────
# DECISION ENGINE
# ─────────────────────────────────────────────
def should_trade(signals):
    t = ENTRY_THRESHOLDS
    sym    = signals.get("symbol", "")
    regime = signals.get("regime", "")

    checks = {
        "opp_score":    signals["opp_score"]    >= t["min_opp_score"],
        "conf_score":   signals["conf_score"]   >= t["min_conf_score"],
        "risk_score":   signals["risk_score"]   <= t["max_risk_score"],
        "volume_ratio": signals["volume_ratio"] >= t["min_volume_ratio"],
        "tf_bullish":   signals["tf_bullish"]   >= t["min_tf_bullish"],

        # Regime quality: current scan must be Strong bull
        "regime_quality": regime == "Strong bull",

        # Regime consistency: previous scan for this symbol must also be
        # Strong bull — prevents entering at the first cycle of a new regime
        "regime_consistency": _last_regime.get(sym) == "Strong bull",

        # Daily trend filter: 1D close must be above the 50-period daily SMA.
        # Backtest finding (Run #2): Feb 2025 produced 0% win rate (6 losses)
        # despite all other filters passing — price was below the daily SMA50
        # in a correction. This filter blocks entries in bear corrections even
        # when short-timeframe signals briefly look bullish.
        # Defaults to True when sma50_d is unavailable (first 50 days of a
        # new asset) so it doesn't silently block legitimate signals.
        "daily_trend": signals.get("daily_trend_ok", True),
    }

    passed  = {k for k, v in checks.items() if v}
    failed  = {k for k, v in checks.items() if not v}
    verdict = len(failed) == 0
    return verdict, passed, failed

# ─────────────────────────────────────────────
# MAIN SCAN CYCLE
# ─────────────────────────────────────────────
def scan_cycle(cycle_num):
    log(f"━━━ Scan cycle #{cycle_num} ━━━", "SCAN")

    # ── P1: Check for closed trades every cycle ──
    # Runs BEFORE the circuit breaker check so that if a position's SL/TP
    # filled while the CB was tripped, the closure still gets recorded and
    # the Telegram summary still fires. Silent on errors — never blocks the
    # main scan loop.
    try:
        closure_resp = fetch(f"{AEGIS_SERVER}/checkclosures")
        if closure_resp and closure_resp.get("closures_found", 0) > 0:
            log(f"Closure check: {closure_resp['closures_found']} trade(s) closed "
                f"— CSV updated, Telegram sent.", "OK")
        # If 0 closures or None (server unreachable), continue silently
    except Exception as e:
        log(f"Closure check error (non-fatal): {e}", "WARN")

    # Check server is alive — retry twice before giving up
    status = None
    for attempt in range(2):
        status = fetch(f"{AEGIS_SERVER}/status")
        if status:
            break
        if attempt == 0:
            log("Server check failed, retrying in 5s...", "WARN")
            time.sleep(5)
    if not status:
        log("Aegis server not reachable after retries. Is aegis_server.py running?", "ERR")
        log("Will retry on next scan cycle.", "WARN")
        return
    if status.get("circuit_breaker"):
        log(f"Circuit breaker active — skipping cycle. Reason: {status.get('cb_reason')}", "WARN")
        return

    global _last_morning_brief_date, _cycle_count_total, _last_regime
    _cycle_count_total += 1
    best_signal = None
    results = []

    for symbol in WATCHLIST:
        signals = calc_signals(symbol)
        if not signals:
            continue

        verdict, passed, failed = should_trade(signals)
        action = "TRADE" if verdict else "WAIT"

        log(f"{symbol} | Opp:{signals['opp_score']} Conf:{signals['conf_score']} "
            f"Risk:{signals['risk_score']} Vol:{signals['volume_ratio']}x "
            f"TF:{signals['tf_bullish']}/3 | {signals['regime']} | → {action}", "INFO")

        if failed:
            log(f"  Failed: {', '.join(sorted(failed))}", "INFO")

        # ── Record this cycle's regime for next cycle's consistency check ──
        _last_regime[symbol] = signals["regime"]

        # Log every signal scan to persistent CSV via server
        post_server("/logsignal", {
            **signals,
            "verdict": action,
            "reasons": list(failed)
        })

        signals["action"] = action
        signals["failed"] = list(failed)
        results.append({**signals, "verdict": action, "passed": list(passed), "failed": list(failed)})

        # AI reasoning for high-scoring setups (score >= 60)
        if signals["opp_score"] >= 60:
            log(f"  AI reasoning {symbol}...", "INFO")
            reasoning = aegis_ai.reason_about_signal(signals)
            log(f"  AI: {reasoning[:180]}{'...' if len(reasoning)>180 else ''}", "INFO")

        if verdict:
            if best_signal is None or signals["opp_score"] > best_signal["opp_score"]:
                best_signal = signals

    # Execute best signal if found
    if best_signal:
        symbol = best_signal["symbol"]
        price  = best_signal["price"]
        stop   = round(price * (1 - STOP_LOSS_PCT),   2)
        target = round(price * (1 + TAKE_PROFIT_PCT), 2)
        rr     = round(TAKE_PROFIT_PCT / STOP_LOSS_PCT, 2)

        log(f"ENTRY SIGNAL: {symbol} @ ${price} | SL:${stop} | TP:${target} | R:R {rr}:1", "TRADE")

        # Validate with server first
        validation = post_server("/validate", {**best_signal, "symbol": symbol})
        if validation and validation.get("valid"):
            # Place the trade
            trade_result = post_server("/trade", {
                **best_signal,
                "symbol":       symbol,
                "side":         "BUY",
                "entry_price":  price,
                "stop_price":   stop,
                "target_price": target,
                "order_type":   "MARKET"
            })
            if trade_result:
                if trade_result.get("status") == "success":
                    log(f"Trade placed successfully: {trade_result.get('order',{}).get('id')}", "OK")
                    # AI commentary on the trade
                    trade_data = {**best_signal, "entry": price, "stop": stop, "target": target}
                    commentary = aegis_ai.comment_on_trade(trade_data)
                    log(f"AI trade commentary: {commentary[:200]}", "INFO")
                    # Telegram alert
                    aegis_ai.alert_trade_fired(trade_data, commentary)
                else:
                    log(f"Trade failed: {trade_result.get('message','unknown')}", "ERR")
        else:
            reasons = validation.get("errors", []) if validation else ["Server validation failed"]
            log(f"Trade blocked by server: {'; '.join(reasons)}", "WARN")
    else:
        log(f"No entry signals this cycle. Watching {len(WATCHLIST)} assets.", "INFO")

    # Generate AI cycle brief every N cycles
    if results and _cycle_count_total % aegis_ai.BRIEF_EVERY_N_CYCLES == 0:
        log("Generating AI market brief...", "INFO")
        brief = aegis_ai.generate_cycle_brief(results, cycle_num)
        log(f"AI Brief: {brief[:200]}{'...' if len(brief)>200 else ''}", "INFO")

    # Morning brief — once per day after 06:00 UTC
    from datetime import timezone as tz
    now_utc = datetime.now(tz.utc).replace(tzinfo=None)
    today = now_utc.date()
    if now_utc.hour >= 6 and _last_morning_brief_date != today and results:
        _last_morning_brief_date = today
        log("Generating morning brief...", "INFO")
        brief = aegis_ai.generate_morning_brief(results)
        log(f"Morning brief: {brief[:200]}{'...' if len(brief)>200 else ''}", "INFO")
        aegis_ai.alert_morning_brief(brief)

    # Anomaly detection every 8 cycles (every 2 hours)
    if _cycle_count_total % 8 == 0:
        try:
            import csv
            signal_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'aegis_signals.csv')
            recent_sigs = []
            if os.path.isfile(signal_file):
                with open(signal_file, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    recent_sigs = list(reader)[-12:]
            if recent_sigs:
                anomaly = aegis_ai.detect_anomalies(recent_sigs)
                if 'No anomalies' not in anomaly:
                    log(f"AI Anomaly: {anomaly}", "WARN")
                    aegis_ai.send_telegram("<b>AEGIS ANOMALY</b>" + chr(10) + str(anomaly))
        except Exception as e:
            log(f"Anomaly check error: {e}", "WARN")

    return results

# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────
def main():
    print("=" * 56)
    print("  Aegis Automated Trading Bot")
    print("=" * 56)
    print(f"  Watchlist    : {', '.join(WATCHLIST)}")
    print(f"  Scan interval: every {SCAN_INTERVAL//60} minutes")
    print(f"  Entry gate   : Opp≥{ENTRY_THRESHOLDS['min_opp_score']} "
          f"Conf≥{ENTRY_THRESHOLDS['min_conf_score']} "
          f"Risk≤{ENTRY_THRESHOLDS['max_risk_score']} "
          f"Vol≥{ENTRY_THRESHOLDS['min_volume_ratio']}x "
          f"| Regime=Strong bull x2 | 1D>SMA50")
    print(f"  Risk per trade: {STOP_LOSS_PCT*100}% SL · {TAKE_PROFIT_PCT*100}% TP → "
          f"{round(TAKE_PROFIT_PCT/STOP_LOSS_PCT,1)}:1 R:R")
    print(f"  Mode         : DEMO (Binance Testnet)")
    print("=" * 56)

    _rotate_old_logs()
    log(f"Bot started — logging to {_bot_log_path()}", "OK")

    # Gap detection — warn if bot was offline for more than one scan cycle
    try:
        import csv as _csv
        _sig_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'aegis_signals.csv')
        if os.path.isfile(_sig_file):
            with open(_sig_file, 'r', encoding='utf-8') as _f:
                _rows = [r for r in _csv.reader(_f) if r and r[0] != 'timestamp']
            if _rows:
                _last_ts = datetime.strptime(_rows[-1][0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                _now = datetime.now(timezone.utc)
                _gap_min = int((_now - _last_ts).total_seconds() / 60)
                if _gap_min > 20:
                    _missed = _gap_min // 15
                    log(f"Bot was offline from {_last_ts.strftime('%Y-%m-%d %H:%M')} UTC to "
                        f"{_now.strftime('%Y-%m-%d %H:%M')} UTC "
                        f"({_gap_min // 60}h {_gap_min % 60}m, ~{_missed} missed scan cycles)", "WARN")
    except Exception:
        pass

    log("Checking Aegis server...", "INFO")
    status = fetch(f"{AEGIS_SERVER}/status")
    if not status:
        print(f"\n  ✗ Cannot reach Aegis server on port {SERVER_PORT}")
        print(f"  ✗ Start aegis_server.py first in another terminal\n")
        return
    log(f"Server OK · {status.get('trades_today',0)} trades today · "
        f"CB: {'active' if status.get('circuit_breaker') else 'clear'}", "OK")

    # Show AI module status
    ai_status = aegis_ai.check_ai_status()
    api_ok = ai_status["api_configured"]
    tg_ok  = ai_status["telegram_configured"]
    log(f"AI module: API {'configured' if api_ok else 'NOT configured — add key to aegis_ai.py'} | Telegram {'enabled' if tg_ok else 'disabled'}",
        "OK" if api_ok else "WARN")

    # Initial scan immediately
    cycle = 1
    scan_cycle(cycle)

    # Then loop
    log(f"Next scan in {SCAN_INTERVAL//60} minutes. Press Ctrl+C to stop.", "INFO")
    while True:
        time.sleep(SCAN_INTERVAL)
        cycle += 1
        scan_cycle(cycle)

if __name__ == "__main__":
    main()

SERVER_PORT = 8888
