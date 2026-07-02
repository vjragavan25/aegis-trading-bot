"""
Aegis Structure Watcher — Phase 1 (structure) + Phase 2 (paper trading)
========================================================================
Single-asset, multi-timeframe market structure watcher.
Detects BOS (Break of Structure) and ChoCH (Change of Character)
on 4 timeframes: 15m, 1H, 2H, 4H.

Phase 2 adds a bidirectional paper-trading signal layer on top of the
structure detection: when 4H+2H recently ChoCH'd the same direction and
at least one of 1H/15m confirms, a structure-derived LONG/SHORT paper
trade is logged with SL/TP taken from confirmed swing points. Outcomes
(WIN/LOSS/TIMEOUT) are tracked every scan and a rolling learning summary
is kept at the bottom of the paper trades file.

NO orders. NO server calls. Read-only. Separate from aegis_reversal_watcher.py.
Output: aegis_structure_log.md, aegis_paper_trades.md
Start: python aegis_structure_watcher.py
"""

import json
import time
import os
import re
from datetime import datetime, timezone
import urllib.request

# ── CONFIGURATION ─────────────────────────────────────────────────────────────

FAPI = "https://fapi.binance.com/fapi/v1"  # futures-only — see Bug #11/#12 in BRIDGE.md

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

INTERVAL_MS = {"15m": 15 * 60_000, "1h": 60 * 60_000, "2h": 2 * 60 * 60_000, "4h": 4 * 60 * 60_000}
STALE_MULT  = 2   # data is STALE if newest candle's close is more than STALE_MULT x interval old

# ── PAPER TRADING (Phase 2) ────────────────────────────────────────────────────
MIN_RR               = 1.5   # minimum reward:risk to log a paper trade
OUTCOME_TIMEOUT_DAYS = 7      # close paper trade as TIMEOUT after N days open

SCRIPT_DIR         = os.path.dirname(os.path.abspath(__file__))
LOG_FILE           = os.path.join(SCRIPT_DIR, "aegis_structure_log.md")
PAPER_TRADES_FILE  = os.path.join(SCRIPT_DIR, "aegis_paper_trades.md")
TREND_TRADES_FILE  = os.path.join(SCRIPT_DIR, "aegis_paper_trades_trend.md")


# ── LOGGING ───────────────────────────────────────────────────────────────────

def log(msg, level="INFO"):
    ts  = datetime.now(timezone.utc).strftime("%H:%M:%S")
    sym = {
        "INFO": ">", "OK": "+", "WARN": "!", "ERR": "X",
        "SCAN": "@", "EV": "*", "ALIGN": "!", "TRADE": "T",
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
    # Futures only — no spot fallback. Spot silently returns stale/delisted
    # candles as a 200 OK (e.g. AERGOUSDT — spot hands back March-2025 candles
    # for "the last 500"), so it's not a safe degrade path; a failed futures
    # call means the caller skips this scan rather than risk corrupt data.
    return fetch(f"{FAPI}/klines?symbol={symbol}&interval={interval}&limit={limit}") or []


def check_staleness(candles, interval):
    """
    Returns (is_stale, gap_ms) based on the newest candle's implied close time
    vs current UTC time. Stale if the gap exceeds STALE_MULT x the interval.
    """
    if not candles:
        return True, None
    interval_ms = INTERVAL_MS.get(interval)
    if not interval_ms:
        return False, None
    last_close_ms = candles[-1]["open_time"] + interval_ms
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    gap_ms = now_ms - last_close_ms
    return gap_ms > STALE_MULT * interval_ms, gap_ms


def validate_symbol(symbol):
    # Futures only — a symbol that exists on spot but not futures is unusable
    # by fetch_klines()/fetch_price() anyway now, so it shouldn't validate.
    t = fetch(f"{FAPI}/ticker/24hr?symbol={symbol}")
    return bool(t and "symbol" in t)


def _ticker_fresh(ticker, max_age_ms=60 * 60_000):
    """Defense-in-depth: reject a ticker whose own closeTime is stale, even
    though the source is futures-only now (a dead/illiquid futures pair could
    still return an old closeTime). Trust it only if the last trade was
    within max_age_ms (default 1h) of now."""
    close_time = ticker.get("closeTime")
    if not close_time:
        return True
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return (now_ms - close_time) <= max_age_ms


def fetch_price(symbol):
    # Futures only — no spot fallback (see fetch_klines()).
    t = fetch(f"{FAPI}/ticker/24hr?symbol={symbol}")
    if t and "lastPrice" in t and _ticker_fresh(t):
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
        log(f"  [{tf['label']}] Futures kline fetch failed for {symbol} — no spot fallback, "
            f"skipping this scan", "ERR")
        return []

    parsed = parse_klines(raw)
    stale, gap_ms = check_staleness(parsed, tf["interval"])
    if stale:
        gap_h = (gap_ms / 3_600_000) if gap_ms is not None else float("inf")
        log(f"  [{tf['label']}] STALE DATA for {symbol} — newest candle is {gap_h:.1f}h old "
            f"(symbol may be delisted/illiquid on this source) — skipping this scan", "WARN")
        return []

    tf["candles"] = merge_candles(tf["candles"], parsed)

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


def get_tf(tf_states, label):
    for tf in tf_states:
        if tf["label"] == label:
            return tf
    return None


def choch_recent(tf, now_ms, window_h=ALIGN_WINDOW_H):
    """True if tf has a ChoCH whose candle open_time is within window_h hours of now."""
    ch = tf["last_choch"]
    if not ch:
        return False
    return (now_ms - ch["open_time"]) <= window_h * 3600 * 1000


def volume_confirmed(tf, choch_event):
    """
    True/False if the ChoCH candle's volume exceeds 1.5x the 20-candle average
    preceding it, None if there isn't enough history to tell.
    """
    if not choch_event:
        return None
    candles = tf["candles"]
    idx = next((i for i, c in enumerate(candles) if c["open_time"] == choch_event["open_time"]), None)
    if idx is None or idx < 20:
        return None
    avg_vol = sum(c["volume"] for c in candles[idx - 20:idx]) / 20
    if avg_vol == 0:
        return None
    return candles[idx]["volume"] > 1.5 * avg_vol


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


# ── PAPER TRADES (Phase 2) — FILE I/O ──────────────────────────────────────────
#
# aegis_paper_trades.md is the ONLY new file this phase writes. There is no
# separate state.json for open trades — on startup we reconstruct the open
# trade list by parsing this file directly (any block with Status: OPEN),
# so a restart doesn't need a second persisted file.

PHASE2_HEADER = (
    "# Aegis Structure Watcher — Paper Trades (Phase 2)\n\n"
    "Bidirectional, structure-derived paper trading signals. "
    "No real orders.\n\n---\n"
)
TREND_HEADER = (
    "# Aegis Structure Watcher — Paper Trades (Trend Continuity Mode)\n\n"
    "Stays positioned with the 4H anchor timeframe, reverses only on a fresh 4H "
    "ChoCH. Parallel comparison to Phase 2 — independent trades, independent "
    "learning summary. No real orders.\n\n---\n"
)


def _ensure_paper_file(file_path, header_text):
    if os.path.exists(file_path):
        return
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(header_text)


def parse_paper_trades(file_path=PAPER_TRADES_FILE):
    """Parse a paper-trades markdown file into a list of trade dicts (best-effort)."""
    if not os.path.exists(file_path):
        return []
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()

    trades = []
    parts = re.split(r"\n## PAPER TRADE #(\d+) — (.+?)\n", text)
    for i in range(1, len(parts), 3):
        tid, ts, body = int(parts[i]), parts[i + 1].strip(), parts[i + 2]

        def grab(pattern):
            m = re.search(pattern, body)
            return m.group(1).strip() if m else None

        asset      = grab(r"Asset\s*:\s*(\S+)")
        direction  = grab(r"Direction\s*:\s*(LONG|SHORT)")
        conf_line  = grab(r"Confidence\s*:\s*([^\n]+)")
        entry      = grab(r"Entry price\s*:\s*\$([\d.]+)")
        sl         = grab(r"Stop loss\s*:\s*\$([\d.]+)")
        tp         = grab(r"Take profit\s*:\s*\$([\d.]+)")
        rr         = grab(r"R:R ratio\s*:\s*([\d.]+)x")
        status     = grab(r"Status\s*:\s*(OPEN|WIN|LOSS|TIMEOUT)")
        btc_regime = grab(r"BTC regime\s*:\s*(\S+)")
        actual_r   = grab(r"Actual R\s*:\s*([+-][\d.]+)x")

        aligned_m     = re.search(r"\((\d)/4 timeframes aligned\)", conf_line or "")
        aligned_count = int(aligned_m.group(1)) if aligned_m else None
        confidence    = conf_line.split("(")[0].strip() if conf_line else None

        try:
            open_epoch = datetime.strptime(ts, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            open_epoch = None

        if not asset or not direction or entry is None or sl is None or tp is None:
            continue

        trades.append({
            "id": tid, "asset": asset, "direction": direction,
            "confidence": confidence, "aligned_count": aligned_count,
            "entry": float(entry), "sl": float(sl), "tp": float(tp),
            "rr": float(rr) if rr else None,
            "open_time_str": ts, "open_time_epoch": open_epoch,
            "status": status or "OPEN", "btc_regime": btc_regime,
            "actual_r": float(actual_r) if actual_r else None,
        })
    return trades


def next_trade_id(trades):
    return (max((t["id"] for t in trades), default=0)) + 1


def append_paper_block(text, file_path=PAPER_TRADES_FILE):
    with open(file_path, "r", encoding="utf-8") as f:
        existing = f.read()
    idx = existing.find("## LEARNING SUMMARY")
    if idx != -1:
        existing = existing[:idx].rstrip() + "\n\n"
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(existing.rstrip("\n") + "\n\n" + text)


def write_paper_trade(trade_id, symbol, direction, confidence, aligned_count,
                       entry, sl, tp, rr, tf_lines, supporting, conflicting,
                       btc_regime, invalidated, file_path=PAPER_TRADES_FILE,
                       header_text=PHASE2_HEADER):
    _ensure_paper_file(file_path, header_text)
    now_str = now_utc_str()
    block = (
        f"## PAPER TRADE #{trade_id} — {now_str}\n\n"
        f"Asset          : {symbol}\n"
        f"Direction      : {direction}\n"
        f"Confidence     : {confidence}  ({aligned_count}/4 timeframes aligned)\n\n"
        f"Entry price    : ${entry:.4f}\n"
        f"Stop loss      : ${sl:.4f}  (4H swing {'low' if direction == 'LONG' else 'high'})\n"
        f"Take profit    : ${tp:.4f}  (4H/2H swing {'high' if direction == 'LONG' else 'low'})\n"
        f"R:R ratio      : {rr:.1f}x : 1\n\n"
        f"Timeframes aligned:\n"
        + "\n".join(tf_lines) + "\n\n"
        f"Reality check:\n"
        f"  Supporting  : {supporting}\n"
        f"  Conflicting : {conflicting}\n"
        f"  BTC regime  : {btc_regime} at signal time\n"
        f"  Invalidated : {invalidated}\n\n"
        f"OUTCOME\n"
        f"  Status       : OPEN\n"
        f"  Close price  : PENDING  (filled when closed)\n"
        f"  Close time   : PENDING\n"
        f"  Actual R     : PENDING  (filled when closed)\n"
        f"  Close reason : PENDING\n\n"
        f"---\n"
    )
    append_paper_block(block, file_path)


def update_paper_trade_outcome(trade_id, status, close_price, close_time_str, actual_r,
                                close_reason, file_path=PAPER_TRADES_FILE):
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    header = f"## PAPER TRADE #{trade_id} —"
    in_block = False
    for i, line in enumerate(lines):
        if line.startswith(header):
            in_block = True
        elif line.startswith("## ") and not line.startswith(header):
            in_block = False
        if not in_block:
            continue
        if line.startswith("  Status       :"):
            lines[i] = f"  Status       : {status}\n"
        elif line.startswith("  Close price  :"):
            lines[i] = f"  Close price  : ${close_price:.4f}\n"
        elif line.startswith("  Close time   :"):
            lines[i] = f"  Close time   : {close_time_str}\n"
        elif line.startswith("  Actual R     :"):
            lines[i] = f"  Actual R     : {actual_r:+.2f}x\n"
        elif line.startswith("  Close reason :"):
            lines[i] = f"  Close reason : {close_reason}\n"
    with open(file_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def compute_learning_stats(trades):
    closed = [t for t in trades if t.get("status") in ("WIN", "LOSS", "TIMEOUT")]

    def bucket(pred):
        sub = [t for t in closed if pred(t)]
        return sum(1 for t in sub if t["status"] == "WIN"), len(sub)

    wins   = [t["actual_r"] for t in closed if t["status"] == "WIN" and t.get("actual_r") is not None]
    losses = [t["actual_r"] for t in closed if t["status"] == "LOSS" and t.get("actual_r") is not None]

    return {
        "long":  bucket(lambda t: t["direction"] == "LONG"),
        "short": bucket(lambda t: t["direction"] == "SHORT"),
        "a2":    bucket(lambda t: t.get("aligned_count") == 2),
        "a3":    bucket(lambda t: t.get("aligned_count") == 3),
        "a4":    bucket(lambda t: t.get("aligned_count") == 4),
        "bull":  bucket(lambda t: t.get("btc_regime") == "BULLISH"),
        "bear":  bucket(lambda t: t.get("btc_regime") == "BEARISH"),
        "side":  bucket(lambda t: t.get("btc_regime") == "SIDEWAYS"),
        "avg_win":  sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
    }


def write_learning_summary(file_path=PAPER_TRADES_FILE):
    if not os.path.exists(file_path):
        return
    stats = compute_learning_stats(parse_paper_trades(file_path))
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    idx = text.find("## LEARNING SUMMARY")
    if idx != -1:
        text = text[:idx].rstrip() + "\n\n"
    lw, lt = stats["long"];  sw, st = stats["short"]
    a2w, a2t = stats["a2"];  a3w, a3t = stats["a3"];  a4w, a4t = stats["a4"]
    bw, bt = stats["bull"];  brw, brt = stats["bear"];  sdw, sdt = stats["side"]
    block = (
        f"## LEARNING SUMMARY (updated {now_utc_str()})\n\n"
        f"Win rate by direction     : LONG {lw}/{lt}  SHORT {sw}/{st}\n"
        f"Win rate by alignment     : 2TF {a2w}/{a2t}  3TF {a3w}/{a3t}  4TF {a4w}/{a4t}\n"
        f"Win rate by BTC regime    : BULLISH {bw}/{bt}  BEARISH {brw}/{brt}  SIDEWAYS {sdw}/{sdt}\n"
        f"Average R on wins         : {stats['avg_win']:+.2f}x\n"
        f"Average R on losses       : {stats['avg_loss']:+.2f}x\n\n"
        f"---\n"
    )
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(text + block)


# ── PAPER TRADES — SHARED SIGNAL HELPERS (used by both Phase 2 and Trend mode) ─

def _fmt_choch(tf):
    ch = tf["last_choch"]
    if not ch:
        return "no ChoCH"
    return f"ChoCH @ ${ch['price']:.4f}  {hhmm(ch['open_time'])} UTC"


def compose_reality_check(tf_states, direction, btc_regime):
    """
    Returns (confidence, aligned_count, supporting_str, conflicting_str, tf_lines)
    describing how many/which of the 4 TFs currently agree with `direction`.
    """
    tf4h, tf2h, tf1h, tf15 = (get_tf(tf_states, l) for l in ("4H", "2H", "1H", "15m"))
    struct_dir = "BULLISH" if direction == "LONG" else "BEARISH"

    aligned_count = sum(1 for tf in (tf4h, tf2h, tf1h, tf15) if tf["structure"] == struct_dir)
    confidence = {4: "VERY HIGH", 3: "HIGH"}.get(aligned_count, "MODERATE")

    supporting, conflicting = [], []
    for tf in (tf4h, tf2h, tf1h, tf15):
        if tf["structure"] == struct_dir:
            supporting.append(f"{tf['label']} {tf['structure']}")
        elif tf["structure"] != "UNDEFINED":
            conflicting.append(f"{tf['label']} {tf['structure']}")

    vol_ok = volume_confirmed(tf4h, tf4h["last_choch"])
    if vol_ok:
        supporting.append("4H ChoCH volume > 1.5x avg (volume confirmed)")
    if btc_regime in ("BULLISH", "BEARISH") and btc_regime != struct_dir:
        conflicting.append(f"BTC regime {btc_regime}")

    supporting_str  = "; ".join(supporting) if supporting else "none"
    conflicting_str = "; ".join(conflicting) if conflicting else "none"

    tf1h_note = "(confirmation)" if tf1h["structure"] == struct_dir else (
        "CONFLICT" if tf1h["structure"] != "UNDEFINED" else "(undefined)")
    tf15_note = "(confirmation)" if tf15["structure"] == struct_dir else (
        "CONFLICT" if tf15["structure"] != "UNDEFINED" else "(undefined)")

    tf_lines = [
        f"  4H: {tf4h['structure']:<9} | {_fmt_choch(tf4h)}",
        f"  2H: {tf2h['structure']:<9} | {_fmt_choch(tf2h)}",
        f"  1H: {tf1h['structure']:<9} | {tf1h_note}",
        f"  15m: {tf15['structure']:<8} | {tf15_note}",
    ]
    return confidence, aligned_count, supporting_str, conflicting_str, tf_lines


def structural_sl_tp(tf_states, direction, current_price):
    """
    SL = the live 4H reference level that invalidates the thesis (last_sl for
    LONG, last_sh for SHORT). TP = nearest confirmed 4H swing beyond entry in
    the trade's favor, falling back to 2H. Returns (sl, tp) or (None, None).
    """
    tf4h, tf2h = get_tf(tf_states, "4H"), get_tf(tf_states, "2H")

    if direction == "LONG":
        sl_ref = tf4h["last_sl"]
        tp_candidates = [s for s in reversed(tf4h["swing_highs"]) if s["price"] > current_price] \
            or [s for s in reversed(tf2h["swing_highs"]) if s["price"] > current_price]
    else:
        sl_ref = tf4h["last_sh"]
        tp_candidates = [s for s in reversed(tf4h["swing_lows"]) if s["price"] < current_price] \
            or [s for s in reversed(tf2h["swing_lows"]) if s["price"] < current_price]

    if not sl_ref or not tp_candidates:
        return None, None
    return sl_ref["price"], tp_candidates[0]["price"]


# ── PAPER TRADES (Phase 2) — SIGNAL + OUTCOME LOGIC ────────────────────────────

def evaluate_paper_signal(symbol, tf_states, btc_regime, open_trades, current_price):
    """
    Checks the bidirectional signal conditions and, if a valid structure-derived
    trade qualifies (RR >= MIN_RR, no conflicting open trade), writes it to
    aegis_paper_trades.md and appends it to open_trades. Always logs exactly
    one console line describing what happened this scan.
    """
    tf4h, tf2h, tf1h, tf15 = (get_tf(tf_states, l) for l in ("4H", "2H", "1H", "15m"))
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    if tf4h["structure"] in ("BULLISH", "BEARISH") and tf2h["structure"] in ("BULLISH", "BEARISH") \
            and tf4h["structure"] != tf2h["structure"]:
        log(f"[PHASE2] No signal this scan (4H:{tf4h['structure'][:4]} 2H:{tf2h['structure'][:4]} — conflict)")
        return

    long_ok = (
        tf4h["structure"] == "BULLISH" and choch_recent(tf4h, now_ms) and tf4h["last_choch"]["direction"] == "Bullish"
        and tf2h["structure"] == "BULLISH" and choch_recent(tf2h, now_ms) and tf2h["last_choch"]["direction"] == "Bullish"
        and (tf1h["structure"] == "BULLISH" or tf15["structure"] == "BULLISH")
    )
    short_ok = (
        tf4h["structure"] == "BEARISH" and choch_recent(tf4h, now_ms) and tf4h["last_choch"]["direction"] == "Bearish"
        and tf2h["structure"] == "BEARISH" and choch_recent(tf2h, now_ms) and tf2h["last_choch"]["direction"] == "Bearish"
        and (tf1h["structure"] == "BEARISH" or tf15["structure"] == "BEARISH")
    )

    direction = "LONG" if long_ok else ("SHORT" if short_ok else None)
    if direction is None:
        log("[PHASE2] No signal this scan (no fresh bidirectional ChoCH alignment)")
        return

    same_open = any(t["asset"] == symbol and t["status"] == "OPEN" and t["direction"] == direction for t in open_trades)
    if same_open:
        log(f"[PHASE2] No signal this scan ({direction} already open)")
        return

    opp_open = any(t["asset"] == symbol and t["status"] == "OPEN" and t["direction"] != direction for t in open_trades)
    if opp_open:
        log("[PHASE2] SIGNAL SUPPRESSED — opposite trade open")
        return

    sl, tp = structural_sl_tp(tf_states, direction, current_price)
    if sl is None or tp is None:
        log(f"[PHASE2] SIGNAL SKIPPED — no valid SL/TP structural level found for {direction}")
        return

    if direction == "LONG":
        risk, reward = current_price - sl, tp - current_price
    else:
        risk, reward = sl - current_price, current_price - tp

    if risk <= 0:
        log(f"[PHASE2] SIGNAL SKIPPED — invalid risk (SL on wrong side of entry) for {direction}")
        return
    rr = reward / risk
    if rr < MIN_RR:
        log(f"[PHASE2] SIGNAL SKIPPED — R:R below minimum ({rr:.1f}x)")
        return

    confidence, aligned_count, supporting_str, conflicting_str, tf_lines = \
        compose_reality_check(tf_states, direction, btc_regime)
    invalidated = f"{'below' if direction == 'LONG' else 'above'} ${sl:.4f}"

    trade_id = next_trade_id(open_trades + parse_paper_trades(PAPER_TRADES_FILE))
    write_paper_trade(trade_id, symbol, direction, confidence, aligned_count,
                       current_price, sl, tp, rr, tf_lines, supporting_str,
                       conflicting_str, btc_regime, invalidated,
                       file_path=PAPER_TRADES_FILE, header_text=PHASE2_HEADER)

    trade = {
        "id": trade_id, "asset": symbol, "direction": direction,
        "confidence": confidence, "aligned_count": aligned_count,
        "entry": current_price, "sl": sl, "tp": tp, "rr": rr,
        "open_time_str": now_utc_str(), "open_time_epoch": time.time(),
        "status": "OPEN", "btc_regime": btc_regime, "actual_r": None,
    }
    open_trades.append(trade)
    log(f"[PHASE2] {direction} signal fired — {symbol} @ ${current_price:.4f}  "
        f"[{aligned_count}/4 TF aligned, R:R {rr:.1f}:1]", "TRADE")


def check_paper_outcomes(open_trades, current_price, file_path=PAPER_TRADES_FILE, mode_label="PHASE2"):
    """For each OPEN trade on this asset, check TP/SL/timeout and close if hit."""
    now = time.time()
    closed_any = False
    for t in open_trades:
        if t["status"] != "OPEN":
            continue
        hit_tp = current_price >= t["tp"] if t["direction"] == "LONG" else current_price <= t["tp"]
        hit_sl = current_price <= t["sl"] if t["direction"] == "LONG" else current_price >= t["sl"]
        age_days = (now - t["open_time_epoch"]) / 86400 if t["open_time_epoch"] else 0

        status, reason = None, None
        if hit_tp:
            status, reason = "WIN", "TP HIT"
        elif hit_sl:
            status, reason = "LOSS", "SL HIT"
        elif age_days > OUTCOME_TIMEOUT_DAYS:
            status, reason = "TIMEOUT", "TIMEOUT"

        if status:
            if status == "LOSS":
                actual_r = -1.0  # SL defines 1R of risk by construction — loss is always -1R
            elif t["direction"] == "LONG":
                actual_r = (current_price - t["entry"]) / (t["entry"] - t["sl"])
            else:
                actual_r = (t["entry"] - current_price) / (t["sl"] - t["entry"])
            close_time = now_utc_str()
            update_paper_trade_outcome(t["id"], status, current_price, close_time, actual_r,
                                        reason, file_path=file_path)
            log(f"[{mode_label}] Paper trade #{t['id']} ({t['asset']} {t['direction']}) closed: "
                f"{status}  {actual_r:+.2f}x  @ ${current_price:.4f}", "TRADE")
            t["status"], t["actual_r"] = status, actual_r
            closed_any = True

    if closed_any:
        write_learning_summary(file_path=file_path)


# ── PAPER TRADES (Trend Continuity Mode) — parallel to Phase 2 ─────────────────

def open_trend_trade(symbol, tf_states, btc_regime, direction, current_price, sl, tp, trend_state):
    if direction == "LONG":
        risk, reward = current_price - sl, tp - current_price
    else:
        risk, reward = sl - current_price, current_price - tp
    rr = (reward / risk) if risk > 0 else 0.0  # no MIN_RR gate — trend mode always takes the anchor's side

    confidence, aligned_count, supporting_str, conflicting_str, tf_lines = \
        compose_reality_check(tf_states, direction, btc_regime)
    invalidated = f"{'below' if direction == 'LONG' else 'above'} ${sl:.4f}"

    trade_id = next_trade_id(parse_paper_trades(TREND_TRADES_FILE))
    write_paper_trade(trade_id, symbol, direction, confidence, aligned_count,
                       current_price, sl, tp, rr, tf_lines, supporting_str,
                       conflicting_str, btc_regime, invalidated,
                       file_path=TREND_TRADES_FILE, header_text=TREND_HEADER)

    trade = {
        "id": trade_id, "asset": symbol, "direction": direction,
        "confidence": confidence, "aligned_count": aligned_count,
        "entry": current_price, "sl": sl, "tp": tp, "rr": rr,
        "open_time_str": now_utc_str(), "open_time_epoch": time.time(),
        "status": "OPEN", "btc_regime": btc_regime, "actual_r": None,
    }
    trend_state["trade"] = trade
    log(f"[TREND] {direction} opened (4H anchor) — {symbol} @ ${current_price:.4f}  "
        f"[{aligned_count}/4 TF aligned, R:R {rr:.1f}:1]", "TRADE")


def close_trend_trade_on_choch(trade, current_price, trend_state):
    """4H ChoCH forces an exit at whatever the real outcome is — not treated as neutral."""
    if trade["direction"] == "LONG":
        actual_r = (current_price - trade["entry"]) / (trade["entry"] - trade["sl"])
    else:
        actual_r = (trade["entry"] - current_price) / (trade["sl"] - trade["entry"])
    status = "WIN" if actual_r >= 0 else "LOSS"
    close_time = now_utc_str()
    update_paper_trade_outcome(trade["id"], status, current_price, close_time, actual_r,
                                "ChoCH forced close", file_path=TREND_TRADES_FILE)
    log(f"[TREND] Paper trade #{trade['id']} ({trade['asset']} {trade['direction']}) closed: "
        f"{status}  {actual_r:+.2f}x  @ ${current_price:.4f}  (ChoCH forced close)", "TRADE")
    trade["status"], trade["actual_r"] = status, actual_r
    write_learning_summary(file_path=TREND_TRADES_FILE)
    trend_state["trade"] = None


def run_trend_mode(symbol, tf_states, btc_regime, trend_state, current_price,
                    fresh_4h_bos, fresh_4h_choch):
    """
    4H-anchored trend-continuity mode: stay positioned with 4H structure, flip
    only on a fresh 4H ChoCH (subject to a 2H conflict check), ignore BOS
    (informational only). At most one open trade at a time.
    """
    tf4h, tf2h = get_tf(tf_states, "4H"), get_tf(tf_states, "2H")
    trade = trend_state.get("trade")

    if trade and trade["status"] == "OPEN":
        wrapper = [trade]
        check_paper_outcomes(wrapper, current_price, file_path=TREND_TRADES_FILE, mode_label="TREND")
        trade = wrapper[0]
        trend_state["trade"] = trade if trade["status"] == "OPEN" else None
        trade = trend_state["trade"]

    if trade and trade["status"] == "OPEN":
        if fresh_4h_choch:
            close_trend_trade_on_choch(trade, current_price, trend_state)
            trade = None
        else:
            if fresh_4h_bos:
                log(f"[TREND] BOS confirms {trade['direction']}, holding")
            else:
                log(f"[TREND] Holding {trade['direction']} — no new 4H ChoCH")
            return

    # No open position (never opened, closed by outcome check, or just ChoCH-closed) — attempt entry
    if tf4h["structure"] == "UNDEFINED":
        log("[TREND] 4H undefined — waiting for structure to resolve")
        return

    target_dir = "LONG" if tf4h["structure"] == "BULLISH" else "SHORT"

    if tf2h["structure"] in ("BULLISH", "BEARISH") and tf2h["structure"] != tf4h["structure"]:
        log(f"[TREND] 4H wants {target_dir}, 2H conflicts — holding flat")
        return

    sl, tp = structural_sl_tp(tf_states, target_dir, current_price)
    if sl is None or tp is None:
        log(f"[TREND] {target_dir} target but no valid SL/TP structural level yet — waiting")
        return

    open_trend_trade(symbol, tf_states, btc_regime, target_dir, current_price, sl, tp, trend_state)


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
    log(f"Paper trades (Phase 2): {PAPER_TRADES_FILE}  |  MIN_RR={MIN_RR}x  |  timeout={OUTCOME_TIMEOUT_DAYS}d")
    log(f"Paper trades (Trend):   {TREND_TRADES_FILE}  |  4H-anchored, no MIN_RR gate")
    log("Press Ctrl+C to stop.")

    write_log(f"# Aegis Structure Log — {symbol}  (started {now_utc_str()})\n")
    _ensure_paper_file(PAPER_TRADES_FILE, PHASE2_HEADER)
    _ensure_paper_file(TREND_TRADES_FILE, TREND_HEADER)

    tf_states      = [init_tf(cfg) for cfg in TIMEFRAMES]
    btc_tf         = None if symbol == "BTCUSDT" else init_tf({"interval": "4h", "label": "BTC-4H"})
    scan_count     = 0
    prev_align_key = None

    open_trades = [t for t in parse_paper_trades(PAPER_TRADES_FILE) if t["asset"] == symbol and t["status"] == "OPEN"]
    if open_trades:
        log(f"[PHASE2] Resumed {len(open_trades)} open paper trade(s) for {symbol} from {PAPER_TRADES_FILE}", "OK")

    trend_open = [t for t in parse_paper_trades(TREND_TRADES_FILE) if t["asset"] == symbol and t["status"] == "OPEN"]
    trend_state = {"trade": trend_open[0] if trend_open else None}
    if trend_state["trade"]:
        log(f"[TREND] Resumed open {trend_state['trade']['direction']} for {symbol} from {TREND_TRADES_FILE}", "OK")

    while True:
        scan_count += 1
        log(f"Scan #{scan_count} — {symbol}", "SCAN")

        all_events = []
        fresh_4h_bos, fresh_4h_choch = None, None
        for tf in tf_states:
            new_events = update_tf(tf, symbol)
            for ev in new_events:
                log(f"[{tf['label']}] NEW {ev['type']} {ev['direction']} "
                    f"@ ${ev['price']:.4f}  (broke ${ev['ref_price']:.4f})", "EV")
                log_event_file(symbol, tf["label"], ev)
                if tf["label"] == "4H":
                    if ev["type"] == "BOS":
                        fresh_4h_bos = ev
                    elif ev["type"] == "ChoCH":
                        fresh_4h_choch = ev
            all_events.extend(new_events)

        if not all_events:
            log("No new structure events this scan.")

        # Multi-TF alignment — only log when it newly appears
        current_price = fetch_price(symbol)

        align = check_alignment(tf_states)
        if align:
            align_key = (align["direction"], tuple(sorted(align["timeframes"])))
            if align_key != prev_align_key and current_price:
                log(f"*** ALIGNMENT [{', '.join(align['timeframes'])}] "
                    f"{align['direction']} @ ${current_price:.4f} ***", "ALIGN")
                log_align_file(symbol, align, current_price)
            prev_align_key = align_key
        else:
            prev_align_key = None

        print_status(tf_states)

        # BTC macro context — 4H structure only, read-only, not logged to file
        if btc_tf is not None:
            update_tf(btc_tf, "BTCUSDT")
            btc_structure = btc_tf["structure"]
        else:
            btc_structure = get_tf(tf_states, "4H")["structure"]
        btc_regime = btc_structure if btc_structure in ("BULLISH", "BEARISH") else "SIDEWAYS"

        # Phase 2 — paper trading: outcome checks, then signal evaluation
        # Trend mode  — parallel, independent open-position tracker (separate file)
        if current_price:
            check_paper_outcomes(open_trades, current_price, file_path=PAPER_TRADES_FILE, mode_label="PHASE2")
            evaluate_paper_signal(symbol, tf_states, btc_regime, open_trades, current_price)

            run_trend_mode(symbol, tf_states, btc_regime, trend_state, current_price,
                           fresh_4h_bos, fresh_4h_choch)
        else:
            log(f"Futures price fetch failed for {symbol} — no spot fallback, "
                f"skipping paper trade checks this scan", "ERR")

        phase2_trades = parse_paper_trades(PAPER_TRADES_FILE)
        n_open   = sum(1 for t in phase2_trades if t["status"] == "OPEN")
        n_closed = sum(1 for t in phase2_trades if t["status"] in ("WIN", "LOSS", "TIMEOUT"))
        n_wins   = sum(1 for t in phase2_trades if t["status"] == "WIN")
        win_rate = (n_wins / n_closed * 100) if n_closed else 0.0
        log(f"[PHASE2] Open paper trades: {n_open}  |  Closed: {n_closed}  |  Win rate: {win_rate:.0f}%")

        trend_trades = parse_paper_trades(TREND_TRADES_FILE)
        tn_open   = sum(1 for t in trend_trades if t["status"] == "OPEN")
        tn_closed = sum(1 for t in trend_trades if t["status"] in ("WIN", "LOSS", "TIMEOUT"))
        tn_wins   = sum(1 for t in trend_trades if t["status"] == "WIN")
        t_win_rate = (tn_wins / tn_closed * 100) if tn_closed else 0.0
        log(f"[TREND] Open paper trades: {tn_open}  |  Closed: {tn_closed}  |  Win rate: {t_win_rate:.0f}%")

        log(f"Sleeping {SCAN_INTERVAL}s...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped by user.", "OK")
