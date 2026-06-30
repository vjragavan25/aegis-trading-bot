"""
Aegis Reversal Short Engine — Observation Phase v2
===================================================
Two-speed architecture:
  Loop A (15min, background thread): Discovers top 3 pumped assets → WATCHING state
  Loop B ( 1min, main thread):       ATR pullback (Stage 2A) + Support break (Stage 2B)
                                     + Signal check (Stage 3) → logs observation

State machine per watched asset:
  WATCHING   → peak tracking, 1-min scans running
  CONFIRMING → Stage 2A passed, awaiting Stage 2B
  OBSERVED   → observation logged, removed from watched_assets

NO orders. NO server calls. NO shared state with aegis_bot.py or aegis_server.py.
Start: python aegis_reversal_watcher.py
"""

import json
import time
import os
import threading
import urllib.request
from datetime import datetime, timezone

# ── CONFIGURATION ─────────────────────────────────────────────────────────────

FAPI_TICKERS   = "https://fapi.binance.com/fapi/v1/ticker/24hr"
SPOT_TICKERS   = "https://api.binance.com/api/v3/ticker/24hr"
SPOT_API       = "https://api.binance.com/api/v3"
FAPI           = "https://fapi.binance.com/fapi/v1"

LOOP_A_INTERVAL = 60 * 15   # 15 minutes — discovery
LOOP_B_INTERVAL = 60         # 1 minute  — watch-mode scan

STABLECOIN_EXCLUDE = {
    "USDCUSDT", "BUSDUSDT", "TUSDUSDT", "USDTUSDT", "WBTCUSDT",
    "WETHUSDT", "STETHUSDT", "FRAXUSDT", "DAIUSDT", "FDUSDUSDT",
}
MIN_VOLUME_USD  = 50_000_000
TOP_N           = 3            # reduced from 5
MIN_24H_GAIN    = 8.0

# Stage 3 — reversal signals
RSI_PERIOD     = 14
RSI_OVERBOUGHT = 70.0
MIN_FUNDING    = 0.06
SMA_PERIOD     = 20
MIN_SIGNALS    = 2

# Stage 2A — ATR-based peak pullback gate
ATR_PERIOD      = 14
ATR_MULTIPLIER  = 1.5
MIN_WATCH_SCANS = 3            # min 1-min scans before Stage 2A can fire

# Stage 2B — support break confirmation
SUPPORT_LOOKBACK  = 48
SUPPORT_PROXIMITY = 0.005      # 0.5% — candle "touches" the level if within this
SUPPORT_MIN_TESTS = 2          # minimum touches to qualify a level
SUPPORT_BUFFER    = 0.99       # close must be < support × 0.99
SUPPORT_VOL_MULT  = 1.5        # volume > 1.5× 20-period avg confirms break

# Outcome tracking
REVERSAL_THRESH    = 3.0
OBS_COOLDOWN_SEC   = 72 * 3600
OUTCOME_4H         = 4  * 3600
OUTCOME_24H        = 24 * 3600
OUTCOME_72H        = 72 * 3600
WATCH_TIMEOUT_DAYS = 7
WATCH_TIMEOUT_H    = WATCH_TIMEOUT_DAYS * 24

SCRIPT_DIR        = os.path.dirname(os.path.abspath(__file__))
OBSERVATIONS_FILE = os.path.join(SCRIPT_DIR, "aegis_reversal_observations.md")
STATE_FILE        = os.path.join(SCRIPT_DIR, "aegis_reversal_state.json")

_fapi_funding_ok = None
_watch_lock      = threading.Lock()  # protects watched_assets between Loop A and Loop B

# ── LOGGING ───────────────────────────────────────────────────────────────────

def log(msg, level="INFO"):
    ts  = datetime.now(timezone.utc).strftime("%H:%M:%S")
    sym = {"INFO": ">", "OK": "+", "WARN": "!", "ERR": "X",
           "OBS": "*", "SCAN": "@", "OUT": "->", "ST": "#"}.get(level, ">")
    line = f"[{ts}] [{sym}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        import sys
        print(line.encode(sys.stdout.encoding or "utf-8", errors="replace")
                  .decode(sys.stdout.encoding or "utf-8", errors="replace"), flush=True)

# ── STATE ─────────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            s.setdefault("watched_assets", {})
            s.setdefault("scan_count_b",  0)
            return s
        except Exception:
            pass
    return {
        "obs_counter":      0,
        "scan_count":       0,
        "scan_count_b":     0,
        "watched_assets":   {},
        "pending_outcomes": [],
        "last_obs_ts":      {},
    }

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log(f"State save failed: {e}", "ERR")

# ── HTTP ──────────────────────────────────────────────────────────────────────

def fetch(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None

def fetch_klines(symbol, interval="1h", limit=60):
    k = fetch(f"{SPOT_API}/klines?symbol={symbol}&interval={interval}&limit={limit}")
    if not k:
        k = fetch(f"{FAPI}/klines?symbol={symbol}&interval={interval}&limit={limit}")
    return k

def fetch_ticker(symbol):
    t = fetch(f"{SPOT_API}/ticker/24hr?symbol={symbol}")
    if not t:
        t = fetch(f"{FAPI}/ticker/24hr?symbol={symbol}")
    return t

def fetch_funding_rate(symbol):
    global _fapi_funding_ok
    if _fapi_funding_ok is False:
        return None
    data = fetch(f"{FAPI}/fundingRate?symbol={symbol}&limit=1", timeout=5)
    if data is None:
        if _fapi_funding_ok is None:
            log("fapi funding rate unreachable — signal will be 'unavailable'", "WARN")
            _fapi_funding_ok = False
        return None
    _fapi_funding_ok = True
    if isinstance(data, list) and data:
        return float(data[0].get("fundingRate", 0)) * 100
    return None

# ── HELPERS ───────────────────────────────────────────────────────────────────

def fmt_vol(v):
    if v >= 1e9: return f"${v/1e9:.1f}B"
    if v >= 1e6: return f"${v/1e6:.0f}M"
    return f"${v/1e3:.0f}K"

def gain_tier(gain_pct):
    if gain_pct > 50:  return "EXTREME"
    if gain_pct > 20:  return "HIGH"
    return "MODERATE"

# ── INDICATORS ────────────────────────────────────────────────────────────────

def sma(values, n):
    if len(values) < n: return None
    return sum(values[-n:]) / n

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]
    ag = sum(gains[:period])  / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0: return 100.0
    return 100.0 - (100.0 / (1.0 + ag / al))

def calc_atr(klines, period=14):
    if not klines or len(klines) < period + 1: return None
    trs = []
    for i in range(1, len(klines)):
        h  = float(klines[i][2])
        l  = float(klines[i][3])
        pc = float(klines[i-1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period: return None
    return sum(trs[-period:]) / period

def calc_support_level(klines):
    """
    Find the most-tested swing low in the last SUPPORT_LOOKBACK candles.
    Returns (price, touch_count) or (None, 0) if none qualifies.
    """
    if not klines or len(klines) < SUPPORT_LOOKBACK + 2:
        return None, 0
    candles = klines[-SUPPORT_LOOKBACK:]
    closes  = [float(c[4]) for c in candles]
    lows    = [float(c[3]) for c in candles]

    swing_lows = []
    for i in range(2, len(lows) - 2):
        if (lows[i] < lows[i-2] and lows[i] < lows[i-1] and
                lows[i] < lows[i+1] and lows[i] < lows[i+2]):
            swing_lows.append(lows[i])

    if not swing_lows:
        return None, 0

    best_level, best_count = None, 0
    for level in swing_lows:
        count = sum(1 for c in closes if abs(c - level) / level <= SUPPORT_PROXIMITY)
        if count > best_count:
            best_count, best_level = count, level

    if best_count >= SUPPORT_MIN_TESTS:
        return best_level, best_count
    return None, 0

def detect_regime(closes):
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    if s20 is None or s50 is None: return "Unknown"
    p = closes[-1]
    if p > s20 > s50: return "Strong bull"
    if p > s20:       return "Weak bull"
    if p < s20 < s50: return "Bearish"
    return "Sideways"

# ── BTC CONTEXT ───────────────────────────────────────────────────────────────

def fetch_btc_regime():
    k = fetch(f"{SPOT_API}/klines?symbol=BTCUSDT&interval=1h&limit=55")
    if not k or len(k) < 50: return "Unknown"
    return detect_regime([float(c[4]) for c in k])

# ── DYNAMIC DISCOVERY ─────────────────────────────────────────────────────────

def fetch_top_performers():
    data   = fetch(FAPI_TICKERS)
    source = "futures"
    if data is None:
        log("fapi ticker unavailable — falling back to spot tickers", "WARN")
        data   = fetch(SPOT_TICKERS)
        source = "spot"
    if data is None:
        log("Both fapi and spot ticker fetch failed — skipping cycle", "ERR")
        return None, None

    candidates = []
    for t in data:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or sym in STABLECOIN_EXCLUDE:
            continue
        gain    = float(t.get("priceChangePercent", 0))
        vol_usd = float(t.get("quoteVolume", 0))
        if gain <= 0 or vol_usd < MIN_VOLUME_USD:
            continue
        candidates.append({
            "symbol":  sym,
            "gain":    gain,
            "vol_usd": vol_usd,
            "price":   float(t.get("lastPrice", 0)),
        })

    candidates.sort(key=lambda x: x["gain"], reverse=True)
    return candidates[:TOP_N], source

# ── STAGE 2A: ATR-BASED PEAK PULLBACK ─────────────────────────────────────────

def check_stage2a(asset, klines, current_price):
    """
    Returns (pass: bool, reason: str).
    Passes when:
      - >= MIN_WATCH_SCANS 1-min scans elapsed since WATCH entry
      - price has pulled back >= 1.5 × ATR14 from tracked_peak
      - RSI(14) is declining (current < 2 candles ago)
    """
    scans = asset.get("watch_scan_count", 0)
    if scans < MIN_WATCH_SCANS:
        return False, f"establishing peak ({scans}/{MIN_WATCH_SCANS} scans)"

    peak  = asset.get("tracked_peak", current_price)
    atr14 = asset.get("atr14")
    if not atr14:
        return False, "ATR14 unavailable"

    pullback  = peak - current_price
    threshold = ATR_MULTIPLIER * atr14
    if pullback < threshold:
        return False, (f"pullback ${pullback:.4f} < {ATR_MULTIPLIER}×ATR ${threshold:.4f} "
                       f"(peak ${peak:.4f})")

    closes   = [float(c[4]) for c in klines]
    rsi_now  = calc_rsi(closes, RSI_PERIOD)
    rsi_prev = calc_rsi(closes[:-2], RSI_PERIOD) if len(closes) > RSI_PERIOD + 2 else None
    if rsi_now is None or rsi_prev is None:
        return False, "RSI unavailable"
    if rsi_now >= rsi_prev:
        return False, f"RSI not declining ({rsi_now:.1f} >= {rsi_prev:.1f})"

    return True, (f"pullback ${pullback:.4f} >= {ATR_MULTIPLIER}×ATR ${threshold:.4f}  "
                  f"RSI {rsi_now:.1f} < {rsi_prev:.1f}")

# ── STAGE 2B: SUPPORT BREAK CONFIRMATION ──────────────────────────────────────

def check_stage2b(asset, klines, current_price):
    """
    Returns (pass: bool, skipped: bool, reason: str).
    skipped=True when no qualified support level exists (Stage 2A alone is sufficient).
    """
    support = asset.get("support_level")
    tests   = asset.get("support_touch_count", 0)

    if support is None or tests < SUPPORT_MIN_TESTS:
        return False, True, "No qualified support level — Stage 2B skipped"

    closes  = [float(c[4]) for c in klines]
    volumes = [float(c[5]) for c in klines]
    avg_vol = sma(volumes[:-1], 20)

    if closes[-1] >= support * SUPPORT_BUFFER:
        return False, False, (f"price ${closes[-1]:.4f} not yet below "
                               f"support ${support:.4f} × {SUPPORT_BUFFER}")

    if avg_vol and volumes[-1] < SUPPORT_VOL_MULT * avg_vol:
        return False, False, (f"low-volume break — vol {volumes[-1]:.0f} < "
                               f"{SUPPORT_VOL_MULT}× avg {avg_vol:.0f}")

    return True, False, (f"closed ${closes[-1]:.4f} below support ${support:.4f}  "
                          f"vol confirmed")

# ── STAGE 3: REVERSAL SIGNALS ─────────────────────────────────────────────────

def check_stage3(klines, funding):
    """Returns (signals dict, sig_count, rsi_curr)."""
    closes  = [float(c[4]) for c in klines]
    volumes = [float(c[5]) for c in klines]
    highs   = [float(c[2]) for c in klines]

    rsi_now  = calc_rsi(closes, RSI_PERIOD)
    rsi_prev = calc_rsi(closes[:-1], RSI_PERIOD) if len(closes) > RSI_PERIOD + 1 else None
    s20_now  = sma(closes,      SMA_PERIOD)
    s20_prev = sma(closes[:-1], SMA_PERIOD)

    high_24 = max(highs[-24:]) if len(highs) >= 24 else highs[-1]
    price   = closes[-1]

    sig_rsi     = (rsi_prev is not None and rsi_prev >= RSI_OVERBOUGHT
                   and rsi_now is not None and rsi_now < RSI_OVERBOUGHT)
    vol_falling = len(volumes) >= 3 and volumes[-3] > volumes[-2] > volumes[-1]
    sig_vol     = vol_falling and price >= high_24 * 0.97
    sig_funding = funding is not None and funding > MIN_FUNDING
    sig_sma     = (s20_prev is not None and closes[-2] >= s20_prev
                   and s20_now is not None and closes[-1] < s20_now)

    signals = {
        "rsi_cross":   sig_rsi,
        "vol_decline": sig_vol,
        "funding":     sig_funding,
        "sma_break":   sig_sma,
    }
    return signals, sum(signals.values()), rsi_now

# ── OBSERVATION FILE ──────────────────────────────────────────────────────────

def _ensure_file():
    if os.path.exists(OBSERVATIONS_FILE):
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(OBSERVATIONS_FILE, "w", encoding="utf-8") as f:
        f.write(
            f"# Aegis Reversal Short Engine — Observation Log\n\n"
            f"**Strategy:** Detect reversal signals on top-performing assets, log outcomes.\n"
            f"**Started:** {today}\n"
            f"**Target:** 30-50 observations before strategy review\n\n"
            f"---\n\n"
        )

def _write_abandoned(symbol, asset):
    """Log an asset to observations file when it exceeds the 7-day WATCH timeout."""
    _ensure_file()
    now_str    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    gain       = asset.get("gain_at_discovery", 0)
    entry      = asset.get("watch_entry_price", 0)
    peak       = asset.get("tracked_peak", entry)
    entry_time = asset.get("watch_entry_time", "unknown")
    state_name = asset.get("state", "WATCHING")
    scans      = asset.get("watch_scan_count", 0)
    block = (
        f"## ABANDONED — {symbol} ({now_str})\n\n"
        f"Asset          : {symbol}\n"
        f"Gain tier      : {gain_tier(gain)}\n"
        f"Discovery gain : +{gain:.1f}%\n"
        f"Watch entry    : {entry_time}  @ ${entry:.4f}\n"
        f"Tracked peak   : ${peak:.4f}\n"
        f"Final state    : {state_name}  (after {scans} 1-min scans)\n"
        f"Abandoned      : {now_str}\n"
        f"Reason         : Watched {WATCH_TIMEOUT_DAYS}+ days without completing Stage 2A/2B/3\n"
        f"---\n\n"
    )
    with open(OBSERVATIONS_FILE, "a", encoding="utf-8") as f:
        f.write(block)
    log(f"ABANDONED: {symbol} — {WATCH_TIMEOUT_DAYS}-day limit reached, logged to observations", "OUT")


def write_observation(obs_id, symbol, asset, signals, sig_count, rsi,
                      funding, regime, btc_regime):
    _ensure_file()
    now_utc       = datetime.now(timezone.utc)
    now_str       = now_utc.strftime("%Y-%m-%d %H:%M UTC")
    entry_price   = asset["current_price"]
    peak          = asset.get("tracked_peak", entry_price)
    peak_to_entry = (entry_price - peak) / peak * 100
    gain          = asset.get("gain_at_discovery", 0)
    vol_usd       = asset.get("vol_usd", 0)
    atr14         = asset.get("atr14")
    support       = asset.get("support_level")
    support_tests = asset.get("support_touch_count", 0)

    def ck(b): return "x" if b else " "
    def due(secs):
        return datetime.fromtimestamp(now_utc.timestamp() + secs,
                                      tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    fund_str = f"{funding:.3f}%" if funding is not None else "unavailable"
    rsi_str  = f"{rsi:.1f}"      if rsi     is not None else "N/A"
    atr_str  = f"${atr14:.4f}"   if atr14   is not None else "N/A"
    sup_str  = f"${support:.4f}" if support  is not None else "Not identified"
    sup_t    = f"{support_tests} times" if support is not None else "N/A"

    block = (
        f"## OBSERVATION #{obs_id} — {now_str}\n\n"
        f"Asset          : {symbol}\n"
        f"Gain tier      : {gain_tier(gain)}\n"
        f"Entry price    : ${entry_price:,.4f}  (price when observation logged)\n"
        f"Peak price     : ${peak:,.4f}  (tracked peak at observation time)\n"
        f"Peak-to-entry  : {peak_to_entry:+.1f}%  (how far price had already fallen from peak)\n"
        f"24h gain       : +{gain:.1f}%\n"
        f"24h volume     : {fmt_vol(vol_usd)}\n"
        f"ATR(14) at entry: {atr_str}\n"
        f"Support level  : {sup_str}\n"
        f"Support tests  : {sup_t}\n\n"
        f"Signals present:\n"
        f"  [{ck(signals['rsi_cross'])}] RSI(1H) crossed below 70  (RSI: {rsi_str})\n"
        f"  [{ck(signals['vol_decline'])}] Volume declining 2+ consecutive candles\n"
        f"  [{ck(signals['funding'])}] Funding rate > +0.06%  (rate: {fund_str})\n"
        f"  [{ck(signals['sma_break'])}] 1H close below 20-period SMA\n"
        f"  Signals count : {sig_count}/4\n\n"
        f"BTC regime     : {btc_regime}\n"
        f"Market regime  : {regime}\n\n"
        f"OUTCOME\n"
        f"  4H   : [PENDING — check {due(OUTCOME_4H)}]\n"
        f"  24H  : [PENDING — check {due(OUTCOME_24H)}]\n"
        f"  72H  : [PENDING — check {due(OUTCOME_72H)}]\n"
        f"  Max reversal : [PENDING]\n\n"
        f"Notes:\n"
        f"---\n\n"
    )

    with open(OBSERVATIONS_FILE, "a", encoding="utf-8") as f:
        f.write(block)

    log(f"OBS #{obs_id} — {symbol} @ ${entry_price:.4f}  peak ${peak:.4f} "
        f"({peak_to_entry:+.1f}%)  {sig_count}/4 signals  "
        f"tier:{gain_tier(gain)}  regime:{regime}", "OBS")


def _update_outcome_line(lines, obs_id, label, pct, note=None):
    header  = f"## OBSERVATION #{obs_id} "
    pad_map = {"4H": "   ", "24H": "  ", "72H": " "}
    padding = pad_map.get(label, " ")
    target  = f"  {label}{padding}: [PENDING"
    pct_str = f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"
    rev_yn  = "YES" if pct <= -REVERSAL_THRESH else "NO"
    note_str = f"  [{note}]" if note else ""
    new_line = (f"  {label}{padding}: {pct_str}  "
                f"(reversal >{REVERSAL_THRESH:.0f}%: {rev_yn}){note_str}\n")
    in_block = False
    for i, line in enumerate(lines):
        if header in line:
            in_block = True
        elif line.startswith("## OBSERVATION #") and header not in line:
            in_block = False
        if in_block and line.startswith(target):
            lines[i] = new_line
            return True
    return False


def _update_max_reversal(lines, obs_id, max_rev):
    header   = f"## OBSERVATION #{obs_id} "
    target   = "  Max reversal : [PENDING]"
    in_block = False
    for i, line in enumerate(lines):
        if header in line:
            in_block = True
        elif line.startswith("## OBSERVATION #") and header not in line:
            in_block = False
        if in_block and line.strip() == target.strip():
            lines[i] = f"  Max reversal : {max_rev:.1f}%\n"
            return True
    return False


def fill_outcome(obs_id, label, pct, max_rev=None, note=None):
    try:
        with open(OBSERVATIONS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        changed = _update_outcome_line(lines, obs_id, label, pct, note=note)
        if max_rev is not None:
            _update_max_reversal(lines, obs_id, max_rev)
        if changed:
            with open(OBSERVATIONS_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines)
            pct_str  = f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"
            rev_note = f"  (max reversal: {max_rev:.1f}%)" if max_rev is not None else ""
            log(f"OBS #{obs_id} {label} outcome: {pct_str}{rev_note}", "OUT")
    except Exception as e:
        log(f"Outcome fill error OBS #{obs_id} {label}: {e}", "ERR")

# ── OUTCOME TRACKING ──────────────────────────────────────────────────────────

def check_pending_outcomes(state, startup=False):
    now     = time.time()
    windows = [("4h", "4H", OUTCOME_4H), ("24h", "24H", OUTCOME_24H), ("72h", "72H", OUTCOME_72H)]
    still_pending = []

    for obs in state["pending_outcomes"]:
        sig_ts   = obs["signal_ts"]
        entry_p  = obs["entry_price"]
        outcomes = obs["outcomes"]

        for key, label, window in windows:
            if outcomes[key] is not None or now - sig_ts < window:
                continue
            ticker = fetch_ticker(obs["symbol"])
            if not ticker:
                log(f"OBS #{obs['obs_id']} ({obs['symbol']}) {label}: ticker fetch failed — retry next cycle", "WARN")
                continue
            curr_p        = float(ticker.get("lastPrice", entry_p))
            pct           = (curr_p - entry_p) / entry_p * 100
            outcomes[key] = pct
            max_rev       = None
            if key == "72h":
                vals    = [v for v in outcomes.values() if v is not None]
                max_rev = min(vals) if vals else None
            note = "estimated — watcher was offline" if startup else None
            fill_outcome(obs["obs_id"], label, pct, max_rev, note=note)

        if any(v is None for v in outcomes.values()):
            still_pending.append(obs)

    state["pending_outcomes"] = still_pending

# ── LOOP A — DISCOVERY (every 15 min, background thread) ──────────────────────

def _run_loop_a(state, btc_regime_cache):
    """Fetch top 3 gainers, update BTC regime, transition new assets to WATCHING."""
    btc_regime_cache["regime"] = fetch_btc_regime()
    log(f"[Loop A] BTC regime (macro context): {btc_regime_cache['regime']}")

    top, source = fetch_top_performers()
    if top is None:
        return

    log(f"[Loop A] Dynamic top {TOP_N} (rolling 24h, source: {source}):")
    now = time.time()

    for rank, perf in enumerate(top, start=1):
        sym  = perf["symbol"]
        gain = perf["gain"]
        vol  = perf["vol_usd"]
        klines = fetch_klines(sym, interval="1h", limit=max(SUPPORT_LOOKBACK + 4, ATR_PERIOD + 4))
        atr14  = calc_atr(klines, ATR_PERIOD) if klines else None
        support, support_tests = calc_support_level(klines) if klines else (None, 0)

        log(f"  #{rank} {sym:<12} {gain:+.1f}%  vol={fmt_vol(vol):<8}  "
            f"atr={f'${atr14:.4f}' if atr14 else 'N/A':<10}  "
            f"sup={f'${support:.4f}' if support else 'none'}")

        if gain < MIN_24H_GAIN:
            continue

        with _watch_lock:
            if sym in state["watched_assets"]:
                # Refresh ATR and support each Loop A cycle
                state["watched_assets"][sym]["atr14"]               = atr14
                state["watched_assets"][sym]["support_level"]       = support
                state["watched_assets"][sym]["support_touch_count"] = support_tests
                continue

            last_obs = state["last_obs_ts"].get(sym, 0)
            if (now - last_obs) < OBS_COOLDOWN_SEC:
                remaining_h = (OBS_COOLDOWN_SEC - (now - last_obs)) / 3600
                log(f"  {sym}: 72h cooldown — {remaining_h:.1f}h remaining")
                continue

            entry_price = perf["price"]
            state["watched_assets"][sym] = {
                "state":                "WATCHING",
                "watch_entry_time":     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "watch_entry_price":    entry_price,
                "tracked_peak":         entry_price,
                "tracked_peak_time":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "atr14":                atr14,
                "support_level":        support,
                "support_touch_count":  support_tests,
                "gain_at_discovery":    gain,
                "vol_usd":              vol,
                "watch_scan_count":     0,
                "current_price":        entry_price,
            }
            log(f"  {sym}: DISCOVERED -> WATCHING  entry=${entry_price:.4f}  "
                f"gain={gain:+.1f}%  tier:{gain_tier(gain)}", "ST")

    # Drop assets: 7-day absolute timeout (any asset) or 4h if no longer in top N
    top_syms = {p["symbol"] for p in top}
    with _watch_lock:
        to_drop = []
        for sym, a in state["watched_assets"].items():
            try:
                watch_start = datetime.strptime(
                    a.get("watch_entry_time", ""), "%Y-%m-%d %H:%M UTC"
                ).replace(tzinfo=timezone.utc)
                age_h = (datetime.now(timezone.utc) - watch_start).total_seconds() / 3600
            except Exception:
                age_h = 0
            if age_h >= WATCH_TIMEOUT_H:
                to_drop.append((sym, "timeout", age_h))
            elif sym not in top_syms and age_h > 4:
                to_drop.append((sym, "dropped", age_h))
        for sym, reason, age_h in to_drop:
            if reason == "timeout":
                _write_abandoned(sym, state["watched_assets"][sym])
            else:
                log(f"  {sym}: not in top {TOP_N} after 4h — WATCHING -> dropped", "ST")
            del state["watched_assets"][sym]

# ── LOOP B — WATCH MODE (every 1 min, main thread) ────────────────────────────

def _run_loop_b(state, btc_regime_cache):
    state["scan_count_b"] = state.get("scan_count_b", 0) + 1

    with _watch_lock:
        watched = dict(state["watched_assets"])

    if not watched:
        log("[Loop B] No assets in WATCH state")
        return

    log(f"[Loop B] {len(watched)} asset(s) in WATCH state")

    for sym, asset in watched.items():
        klines = fetch_klines(sym, interval="1h", limit=60)
        if not klines or len(klines) < SMA_PERIOD + RSI_PERIOD + 2:
            log(f"  {sym}: klines unavailable — skipping", "WARN")
            continue

        ticker = fetch_ticker(sym)
        if not ticker:
            log(f"  {sym}: ticker unavailable — skipping", "WARN")
            continue

        current_price = float(ticker.get("lastPrice", float(klines[-1][4])))
        funding       = fetch_funding_rate(sym)

        # Update peak + scan count under lock
        with _watch_lock:
            if sym not in state["watched_assets"]:
                continue  # removed by Loop A between snapshot and now
            wa = state["watched_assets"][sym]
            wa["current_price"]    = current_price
            wa["watch_scan_count"] = wa.get("watch_scan_count", 0) + 1
            if current_price > wa.get("tracked_peak", 0):
                wa["tracked_peak"]      = current_price
                wa["tracked_peak_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            new_atr = calc_atr(klines, ATR_PERIOD)
            if new_atr:
                wa["atr14"] = new_atr
            asset = dict(wa)  # refreshed copy for checks below

        peak    = asset.get("tracked_peak", current_price)
        atr14   = asset.get("atr14")
        atr_str = f"${atr14:.4f}" if atr14 else "N/A"

        log(f"  {sym} [{asset['state']}]: price=${current_price:.4f}  "
            f"peak=${peak:.4f}  atr={atr_str}  "
            f"scans={asset.get('watch_scan_count', 0)}")

        # ── Stage 2A ──────────────────────────────────────────────────────
        s2a_pass, s2a_reason = check_stage2a(asset, klines, current_price)
        log(f"    Stage 2A: {'PASS' if s2a_pass else 'WAIT'} — {s2a_reason}")
        if not s2a_pass:
            continue

        # ── Stage 2B ──────────────────────────────────────────────────────
        s2b_pass, s2b_skip, s2b_reason = check_stage2b(asset, klines, current_price)
        log(f"    Stage 2B: {'PASS' if s2b_pass else ('SKIP' if s2b_skip else 'WAIT')} — {s2b_reason}")

        if not s2b_pass and not s2b_skip:
            # Mark CONFIRMING if not already
            with _watch_lock:
                if sym in state["watched_assets"]:
                    if state["watched_assets"][sym]["state"] != "CONFIRMING":
                        state["watched_assets"][sym]["state"] = "CONFIRMING"
                        log(f"  {sym}: WATCHING -> CONFIRMING (2A passed, awaiting 2B)", "ST")
            continue

        # ── Stage 3 ───────────────────────────────────────────────────────
        signals, sig_count, rsi = check_stage3(klines, funding)
        log(f"    Stage 3: {sig_count}/4 — "
            f"rsi:{signals['rsi_cross']} vol:{signals['vol_decline']} "
            f"fund:{signals['funding']} sma:{signals['sma_break']}")

        if sig_count < MIN_SIGNALS:
            log(f"    {sym}: {sig_count}/4 signals — need {MIN_SIGNALS}")
            continue

        # ── All stages passed → log observation ───────────────────────────
        closes    = [float(c[4]) for c in klines]
        regime    = detect_regime(closes)
        btc_regime = btc_regime_cache.get("regime", "Unknown")

        with _watch_lock:
            if sym not in state["watched_assets"]:
                continue
            state["obs_counter"] += 1
            obs_id = state["obs_counter"]
            asset["current_price"] = current_price
            log(f"  {sym}: CONFIRMING -> OBSERVED  (all stages passed)", "ST")
            del state["watched_assets"][sym]

        write_observation(obs_id, sym, asset, signals, sig_count, rsi,
                          funding, regime, btc_regime)

        now_ts = time.time()
        state["pending_outcomes"].append({
            "obs_id":          obs_id,
            "symbol":          sym,
            "entry_price":     current_price,
            "entry_time":      datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "signal_ts":       now_ts,
            "signals_present": [k for k, v in signals.items() if v],
            "gain_at_entry":   asset.get("gain_at_discovery", 0),
            "outcomes":        {"4h": None, "24h": None, "72h": None},
        })
        state["last_obs_ts"][sym] = now_ts

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    log("Aegis Reversal Short Engine — Observation Phase v2")
    log("NO orders. NO server calls. Pure read-only observation.")
    log(f"Loop A (discovery): every {LOOP_A_INTERVAL // 60} min  |  "
        f"Loop B (watch):     every {LOOP_B_INTERVAL}s")
    log(f"TOP_N={TOP_N}  MIN_GAIN={MIN_24H_GAIN}%  "
        f"ATR_MULT={ATR_MULTIPLIER}x  MIN_SIGNALS={MIN_SIGNALS}/4")

    state = load_state()
    log(f"State loaded — obs#{state['obs_counter']}  "
        f"watching:{len(state['watched_assets'])}  "
        f"pending_outcomes:{len(state['pending_outcomes'])}", "OK")

    if state["pending_outcomes"]:
        log("Checking for outcomes elapsed while offline...")
        check_pending_outcomes(state, startup=True)
        save_state(state)

    _ensure_file()

    btc_regime_cache = {"regime": "Unknown"}

    # Run Loop A once immediately before starting background thread
    log("Running initial Loop A discovery...")
    try:
        _run_loop_a(state, btc_regime_cache)
        save_state(state)
    except Exception as e:
        log(f"Initial Loop A error: {e}", "ERR")

    def loop_a_thread():
        while True:
            time.sleep(LOOP_A_INTERVAL)
            try:
                _run_loop_a(state, btc_regime_cache)
                save_state(state)
            except Exception as e:
                log(f"Loop A error: {e}", "ERR")

    t = threading.Thread(target=loop_a_thread, daemon=True, name="LoopA")
    t.start()
    log("Loop A background thread started.", "OK")
    log("Starting Loop B (1-min watch scans)...", "OK")

    while True:
        try:
            _run_loop_b(state, btc_regime_cache)
            check_pending_outcomes(state)
            with _watch_lock:
                n_watch = len(state["watched_assets"])
            n_open = len(state["pending_outcomes"])
            n_4h   = sum(1 for o in state["pending_outcomes"] if o["outcomes"]["4h"]  is None)
            n_24h  = sum(1 for o in state["pending_outcomes"] if o["outcomes"]["24h"] is None)
            n_72h  = sum(1 for o in state["pending_outcomes"] if o["outcomes"]["72h"] is None)
            log(f"Watching: {n_watch}  Tracking: {n_open} open  "
                f"Pending outcomes: 4h={n_4h} 24h={n_24h} 72h={n_72h}")
            save_state(state)
        except Exception as e:
            log(f"Loop B error: {e}", "ERR")
        log(f"Loop B sleeping {LOOP_B_INTERVAL}s...")
        time.sleep(LOOP_B_INTERVAL)


if __name__ == "__main__":
    main()
