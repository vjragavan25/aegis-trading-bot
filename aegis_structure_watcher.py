"""
Aegis Structure Watcher — Phase 1: Market Structure Detection
=============================================================
Single-asset, multi-timeframe market structure watcher.
Detects BOS (Break of Structure) and ChoCH (Change of Character)
on 4 timeframes: 15m, 1H, 2H, 4H.

NO orders. NO server calls. Read-only. Separate from aegis_reversal_watcher.py.
Output: aegis_structure_log.md
Start: python aegis_structure_watcher.py
"""

import json
import time
import os
from datetime import datetime, timezone
import urllib.request

# ── CONFIGURATION ─────────────────────────────────────────────────────────────

SPOT_API = "https://api.binance.com/api/v3"
FAPI     = "https://fapi.binance.com/fapi/v1"

TIMEFRAMES = [
    {"interval": "15m", "label": "15m"},
    {"interval": "1h",  "label": "1H"},
    {"interval": "2h",  "label": "2H"},
    {"interval": "4h",  "label": "4H"},
]

SWING_N        = 2    # candles on each side for swing point confirmation
INITIAL_LIMIT  = 200  # candles to load at startup per TF
REFRESH_LIMIT  = 10   # candles to fetch each scan
SCAN_INTERVAL  = 60   # seconds between scans
ALIGN_WINDOW_H = 4    # hours — ChoCH must be within this window to count as aligned

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE   = os.path.join(SCRIPT_DIR, "aegis_structure_log.md")


# ── LOGGING ───────────────────────────────────────────────────────────────────

def log(msg, level="INFO"):
    ts  = datetime.now(timezone.utc).strftime("%H:%M:%S")
    sym = {
        "INFO": ">", "OK": "+", "WARN": "!", "ERR": "X",
        "SCAN": "@", "EV": "*", "ALIGN": "!",
    }.get(level, ">")
    line = f"[{ts}] [{sym}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        import sys
        print(line.encode("utf-8", errors="replace").decode("utf-8", errors="replace"),
              flush=True)


def write_log(text):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception as e:
        log(f"Log write failed: {e}", "ERR")


# ── HTTP ──────────────────────────────────────────────────────────────────────

def fetch(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AegisStructureWatcher/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def fetch_klines(symbol, interval, limit):
    data = fetch(f"{SPOT_API}/klines?symbol={symbol}&interval={interval}&limit={limit}")
    if not data:
        data = fetch(f"{FAPI}/klines?symbol={symbol}&interval={interval}&limit={limit}")
    return data or []


def validate_symbol(symbol):
    t = fetch(f"{SPOT_API}/ticker/24hr?symbol={symbol}")
    if t and "symbol" in t:
        return True
    t = fetch(f"{FAPI}/ticker/24hr?symbol={symbol}")
    return bool(t and "symbol" in t)


def fetch_price(symbol):
    t = fetch(f"{SPOT_API}/ticker/24hr?symbol={symbol}")
    if t and "lastPrice" in t:
        return float(t["lastPrice"])
    t = fetch(f"{FAPI}/ticker/24hr?symbol={symbol}")
    if t and "lastPrice" in t:
        return float(t["lastPrice"])
    return None


# ── CANDLE HELPERS ────────────────────────────────────────────────────────────

def parse_klines(raw):
    return [
        {
            "open_time": int(k[0]),
            "open":      float(k[1]),
            "high":      float(k[2]),
            "low":       float(k[3]),
            "close":     float(k[4]),
            "volume":    float(k[5]),
        }
        for k in raw
    ]


def merge_candles(buf, new_candles, max_size=200):
    seen = {c["open_time"] for c in buf}
    for c in new_candles:
        if c["open_time"] not in seen:
            buf.append(c)
            seen.add(c["open_time"])
    buf.sort(key=lambda c: c["open_time"])
    return buf[-max_size:]


def closed_candles(buf):
    """All candles except the last (still-forming) one."""
    return buf[:-1] if len(buf) > 1 else []


# ── SWING DETECTION ───────────────────────────────────────────────────────────

def find_swings(candles, n=2):
    """
    Returns (highs, lows) as lists of {price, open_time}.
    A swing high at index i: its high exceeds the n candles before AND after it.
    A swing low at index i: its low is below the n candles before AND after it.
    Candles at positions 0..(n-1) and (len-n)..end cannot be confirmed.
    """
    highs, lows = [], []
    for i in range(n, len(candles) - n):
        h = candles[i]["high"]
        l = candles[i]["low"]
        if all(h > candles[i - j]["high"] for j in range(1, n + 1)) and \
           all(h > candles[i + j]["high"] for j in range(1, n + 1)):
            highs.append({"price": h, "open_time": candles[i]["open_time"]})
        if all(l < candles[i - j]["low"] for j in range(1, n + 1)) and \
           all(l < candles[i + j]["low"] for j in range(1, n + 1)):
            lows.append({"price": l, "open_time": candles[i]["open_time"]})
    return highs, lows


def classify_structure(swing_highs, swing_lows):
    """BULLISH = HH+HL, BEARISH = LH+LL, UNDEFINED otherwise."""
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return "UNDEFINED"
    hh = swing_highs[-1]["price"] > swing_highs[-2]["price"]
    hl = swing_lows[-1]["price"]  > swing_lows[-2]["price"]
    lh = swing_highs[-1]["price"] < swing_highs[-2]["price"]
    ll = swing_lows[-1]["price"]  < swing_lows[-2]["price"]
    if hh and hl:
        return "BULLISH"
    if lh and ll:
        return "BEARISH"
    return "UNDEFINED"


# ── PER-TF STATE ──────────────────────────────────────────────────────────────

def init_tf(cfg):
    return {
        "interval":      cfg["interval"],
        "label":         cfg["label"],
        "candles":       [],
        "swing_highs":   [],   # confirmed swing highs (list of {price, open_time})
        "swing_lows":    [],   # confirmed swing lows
        "structure":     "UNDEFINED",
        "struct_locked": False,  # True once initial structure established via swing classification
        "last_sh":       None,   # {price, open_time} — reference level for BOS/ChoCH checks
        "last_sl":       None,
        "last_bos":      None,   # {direction, price, ref_price, ref_time, open_time}
        "last_choch":    None,
        "seen_ot":       set(),  # open_times of candles already processed for events
    }


# ── EVENT DETECTION ───────────────────────────────────────────────────────────

def detect_events(tf):
    """
    Check each unprocessed closed candle for BOS / ChoCH against current reference levels.
    Updates tf in-place. Returns list of new event dicts.

    BOS in BULLISH:  close > last_sh → trend continuation, update last_sh to candle high
    ChoCH in BULLISH: close < last_sl → reversal, flip structure to BEARISH
    BOS in BEARISH:  close < last_sl → trend continuation, update last_sl to candle low
    ChoCH in BEARISH: close > last_sh → reversal, flip structure to BULLISH
    """
    events  = []
    closed  = closed_candles(tf["candles"])

    for c in closed:
        ot = c["open_time"]
        if ot in tf["seen_ot"]:
            continue
        tf["seen_ot"].add(ot)

        # Re-read per-iteration: structure and refs may have changed from earlier candle
        structure = tf["structure"]
        last_sh   = tf["last_sh"]
        last_sl   = tf["last_sl"]

        if structure == "UNDEFINED" or not last_sh or not last_sl:
            continue

        close = c["close"]

        if structure == "BULLISH":
            if close > last_sh["price"]:
                ev = {
                    "type": "BOS", "direction": "Bullish",
                    "price": close, "ref_price": last_sh["price"],
                    "ref_time": last_sh["open_time"], "open_time": ot,
                }
                events.append(ev)
                tf["last_bos"] = ev
                # New reference: this candle's high becomes the level to beat next
                tf["last_sh"] = {"price": c["high"], "open_time": ot}

            elif close < last_sl["price"]:
                ev = {
                    "type": "ChoCH", "direction": "Bearish",
                    "price": close, "ref_price": last_sl["price"],
                    "ref_time": last_sl["open_time"], "open_time": ot,
                }
                events.append(ev)
                tf["last_choch"] = ev
                tf["structure"]  = "BEARISH"
                # Advance last_sl to ChoCH candle's low so next BOS must break further
                tf["last_sl"] = {"price": c["low"], "open_time": ot}

        elif structure == "BEARISH":
            if close < last_sl["price"]:
                ev = {
                    "type": "BOS", "direction": "Bearish",
                    "price": close, "ref_price": last_sl["price"],
                    "ref_time": last_sl["open_time"], "open_time": ot,
                }
                events.append(ev)
                tf["last_bos"] = ev
                # New reference: this candle's low becomes the level to break next
                tf["last_sl"] = {"price": c["low"], "open_time": ot}

            elif close > last_sh["price"]:
                ev = {
                    "type": "ChoCH", "direction": "Bullish",
                    "price": close, "ref_price": last_sh["price"],
                    "ref_time": last_sh["open_time"], "open_time": ot,
                }
                events.append(ev)
                tf["last_choch"] = ev
                tf["structure"]  = "BULLISH"
                # Advance last_sh to ChoCH candle's high so next BOS must break further
                tf["last_sh"] = {"price": c["high"], "open_time": ot}

    return events


# ── INITIAL BOOTSTRAP ────────────────────────────────────────────────────────

def _bootstrap_initial(tf, closed, swing_highs, swing_lows):
    """
    Replay 200-candle history sequentially, releasing swing confirmations only
    as each candle arrives — no look-ahead bias.

    Root cause this fixes: if find_swings() pre-sets last_sh to a pump candle's
    high ($0.068) before detect_events() runs, the pump candle's close can never
    exceed last_sh (it closes AT the high, not above it), so the ChoCH that should
    flip structure to BULLISH never fires. Sequential replay avoids this by setting
    last_sh from the PRE-PUMP swing high when the pump candle is processed.
    """
    n = SWING_N
    ot_to_pos = {c["open_time"]: i for i, c in enumerate(closed)}

    sh_idx = sl_idx = 0
    acc_sh, acc_sl = [], []
    structure = "UNDEFINED"
    last_sh = last_sl = None

    for i, c in enumerate(closed):
        ot = c["open_time"]
        tf["seen_ot"].add(ot)

        # Release swings whose n-candle confirmation window has elapsed (j + n <= i)
        while sh_idx < len(swing_highs):
            sh = swing_highs[sh_idx]
            j  = ot_to_pos.get(sh["open_time"])
            if j is None:
                sh_idx += 1
                continue
            if j + n > i:
                break
            acc_sh.append(sh)
            if last_sh is None or sh["open_time"] > last_sh["open_time"]:
                last_sh = sh
            sh_idx += 1

        while sl_idx < len(swing_lows):
            sl = swing_lows[sl_idx]
            j  = ot_to_pos.get(sl["open_time"])
            if j is None:
                sl_idx += 1
                continue
            if j + n > i:
                break
            acc_sl.append(sl)
            if last_sl is None or sl["open_time"] > last_sl["open_time"]:
                last_sl = sl
            sl_idx += 1

        # Establish structure when first 2 swings of each type are confirmed
        if structure == "UNDEFINED" and len(acc_sh) >= 2 and len(acc_sl) >= 2:
            s = classify_structure(acc_sh, acc_sl)
            if s != "UNDEFINED":
                structure = s

        if structure == "UNDEFINED" or last_sh is None or last_sl is None:
            continue

        close = c["close"]

        if structure == "BULLISH":
            if close > last_sh["price"]:
                tf["last_bos"] = {
                    "type": "BOS", "direction": "Bullish",
                    "price": close, "ref_price": last_sh["price"],
                    "ref_time": last_sh["open_time"], "open_time": ot,
                }
                last_sh = {"price": c["high"], "open_time": ot}
            elif close < last_sl["price"]:
                tf["last_choch"] = {
                    "type": "ChoCH", "direction": "Bearish",
                    "price": close, "ref_price": last_sl["price"],
                    "ref_time": last_sl["open_time"], "open_time": ot,
                }
                structure = "BEARISH"
                last_sl   = {"price": c["low"], "open_time": ot}

        elif structure == "BEARISH":
            if close < last_sl["price"]:
                tf["last_bos"] = {
                    "type": "BOS", "direction": "Bearish",
                    "price": close, "ref_price": last_sl["price"],
                    "ref_time": last_sl["open_time"], "open_time": ot,
                }
                last_sl = {"price": c["low"], "open_time": ot}
            elif close > last_sh["price"]:
                tf["last_choch"] = {
                    "type": "ChoCH", "direction": "Bullish",
                    "price": close, "ref_price": last_sh["price"],
                    "ref_time": last_sh["open_time"], "open_time": ot,
                }
                structure = "BULLISH"
                last_sh   = {"price": c["high"], "open_time": ot}

    tf["swing_highs"]   = acc_sh
    tf["swing_lows"]    = acc_sl
    tf["structure"]     = structure
    tf["struct_locked"] = (structure != "UNDEFINED")
    tf["last_sh"]       = last_sh
    tf["last_sl"]       = last_sl


# ── UPDATE TF ─────────────────────────────────────────────────────────────────

def update_tf(tf, symbol):
    """Fetch candles, run swing detection, classify structure, detect events."""
    initial = not tf["candles"]
    if initial:
        raw = fetch_klines(symbol, tf["interval"], INITIAL_LIMIT)
        if raw:
            log(f"  [{tf['label']}] Loaded {len(raw)} candles (initial)")
    else:
        raw = fetch_klines(symbol, tf["interval"], REFRESH_LIMIT)

    if not raw:
        log(f"  [{tf['label']}] Kline fetch failed", "WARN")
        return []

    tf["candles"] = merge_candles(tf["candles"], parse_klines(raw))

    closed = closed_candles(tf["candles"])
    if len(closed) < SWING_N * 2 + 1:
        return []

    highs, lows = find_swings(closed, SWING_N)

    if initial:
        # Replay history candle-by-candle to avoid look-ahead bias.
        # (Pre-setting last_sh from find_swings() would cause a pump candle's own
        # high to become last_sh, preventing the ChoCH it created from being detected.)
        _bootstrap_initial(tf, closed, highs, lows)
        return []

    tf["swing_highs"] = highs
    tf["swing_lows"]  = lows

    # Update references from newly confirmed swings (preserves BOS-set refs)
    if highs:
        sh = highs[-1]
        if tf["last_sh"] is None or sh["open_time"] > tf["last_sh"]["open_time"]:
            tf["last_sh"] = sh
    if lows:
        sl = lows[-1]
        if tf["last_sl"] is None or sl["open_time"] > tf["last_sl"]["open_time"]:
            tf["last_sl"] = sl

    if not tf["struct_locked"]:
        detected = classify_structure(highs, lows)
        if detected != "UNDEFINED":
            tf["structure"]     = detected
            tf["struct_locked"] = True

    known_ots = {c["open_time"] for c in tf["candles"]}
    tf["seen_ot"] &= known_ots

    return detect_events(tf)


# ── MULTI-TF ALIGNMENT ────────────────────────────────────────────────────────

def check_alignment(tf_states):
    """
    Returns alignment dict if 2+ TFs show ChoCH in the same direction
    within the last ALIGN_WINDOW_H hours, else None.
    """
    now_ms    = int(datetime.now(timezone.utc).timestamp() * 1000)
    window_ms = ALIGN_WINDOW_H * 3600 * 1000
    bullish, bearish = [], []

    for tf in tf_states:
        choch = tf["last_choch"]
        if not choch:
            continue
        if now_ms - choch["open_time"] > window_ms:
            continue
        if choch["direction"] == "Bullish":
            bullish.append(tf["label"])
        elif choch["direction"] == "Bearish":
            bearish.append(tf["label"])

    if len(bullish) >= 2:
        return {"direction": "Bullish", "timeframes": bullish}
    if len(bearish) >= 2:
        return {"direction": "Bearish", "timeframes": bearish}
    return None


# ── FORMATTING ────────────────────────────────────────────────────────────────

def ts_utc(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def hhmm(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%H:%M")

def now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ── FILE LOGGING ──────────────────────────────────────────────────────────────

def log_event_file(symbol, tf_label, ev):
    # Bullish events always break a swing high; bearish events always break a swing low
    ref_type = "high" if ev["direction"] == "Bullish" else "low"
    block = (
        f"\n## EVENT — {now_utc_str()}\n\n"
        f"Asset       : {symbol}\n"
        f"Timeframe   : {tf_label}\n"
        f"Event       : {ev['type']}\n"
        f"Direction   : {ev['direction']}\n"
        f"Price       : ${ev['price']:.4f}\n"
        f"Swing ref   : previous swing {ref_type} @ ${ev['ref_price']:.4f}"
        f"  ({ts_utc(ev['ref_time'])})\n\n"
        f"---"
    )
    write_log(block)


def log_align_file(symbol, align, price):
    tfs = ", ".join(align["timeframes"])
    block = (
        f"\n## ALIGNMENT — {now_utc_str()}  [HIGH PRIORITY]\n\n"
        f"Asset            : {symbol}\n"
        f"Timeframes aligned: [{tfs}]\n"
        f"Direction        : {align['direction']}\n"
        f"Price at moment  : ${price:.4f}\n\n"
        f"---"
    )
    write_log(block)


# ── CONSOLE STATUS ────────────────────────────────────────────────────────────

def print_status(tf_states):
    for tf in tf_states:
        label  = tf["label"]
        struct = tf["structure"]
        bos    = tf["last_bos"]
        choch  = tf["last_choch"]
        n_sh   = len(tf["swing_highs"])
        n_sl   = len(tf["swing_lows"])

        if struct == "UNDEFINED":
            detail = f"building structure ({min(n_sh, n_sl)}/2 swing points)"
        else:
            bos_str   = (f"last BOS {hhmm(bos['open_time'])} @ ${bos['price']:.4f}"
                         if bos else "no BOS yet")
            choch_str = (f"last ChoCH {hhmm(choch['open_time'])} @ ${choch['price']:.4f}"
                         if choch else "no ChoCH yet")
            detail = f"{bos_str} | {choch_str}"

        log(f"  {label:<3}: {struct:<9} | {detail}")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    symbol = input("Enter asset symbol to watch (e.g. BTCUSDT): ").strip().upper()
    if not symbol:
        print("No symbol entered. Exiting.")
        return

    log(f"Validating {symbol}...")
    if not validate_symbol(symbol):
        log(f"Symbol {symbol} not found on Binance (spot or futures). Exiting.", "ERR")
        return

    log(f"Aegis Structure Watcher — {symbol}", "SCAN")
    log(f"Timeframes: 15m | 1H | 2H | 4H  |  Swing N={SWING_N}  |  Scan every {SCAN_INTERVAL}s")
    log(f"Output: {LOG_FILE}")
    log("Press Ctrl+C to stop.")

    write_log(f"# Aegis Structure Log — {symbol}  (started {now_utc_str()})\n")

    tf_states      = [init_tf(cfg) for cfg in TIMEFRAMES]
    scan_count     = 0
    prev_align_key = None

    while True:
        scan_count += 1
        log(f"Scan #{scan_count} — {symbol}", "SCAN")

        all_events = []
        for tf in tf_states:
            new_events = update_tf(tf, symbol)
            for ev in new_events:
                log(f"[{tf['label']}] NEW {ev['type']} {ev['direction']} "
                    f"@ ${ev['price']:.4f}  (broke ${ev['ref_price']:.4f})", "EV")
                log_event_file(symbol, tf["label"], ev)
            all_events.extend(new_events)

        if not all_events:
            log("No new structure events this scan.")

        # Multi-TF alignment — only log when it newly appears
        align = check_alignment(tf_states)
        if align:
            align_key = (align["direction"], tuple(sorted(align["timeframes"])))
            if align_key != prev_align_key:
                price = fetch_price(symbol)
                if price:
                    log(f"*** ALIGNMENT [{', '.join(align['timeframes'])}] "
                        f"{align['direction']} @ ${price:.4f} ***", "ALIGN")
                    log_align_file(symbol, align, price)
            prev_align_key = align_key
        else:
            prev_align_key = None

        print_status(tf_states)
        log(f"Sleeping {SCAN_INTERVAL}s...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped by user.", "OK")
