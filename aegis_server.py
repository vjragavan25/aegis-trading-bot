"""
Aegis Trading Server — Local Signing Bridge
============================================
Runs on your machine. Handles HMAC-SHA256 signing for Binance API.
Connects the Aegis intelligence engine to Binance Demo Trading.

Start: python aegis_server.py
Stop:  Ctrl+C

NEVER share this file if your real API keys are inside.
This server runs on localhost only — not accessible from the internet.
"""

import hmac
import sys
import csv
import os
import sqlite3
import hashlib
import time
import json
import urllib.parse
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

# ─────────────────────────────────────────────
# CONFIGURATION — paste your Demo API keys here
# ─────────────────────────────────────────────
try:
    from aegis_secrets import BINANCE_API_KEY as API_KEY, BINANCE_SECRET_KEY as SECRET_KEY
except ImportError:
    API_KEY    = "PASTE_YOUR_DEMO_API_KEY_HERE"
    SECRET_KEY = "PASTE_YOUR_DEMO_SECRET_KEY_HERE"

# Binance Demo endpoints
REST_BASE  = "https://demo-api.binance.com/api/v3"  # Updated: Binance migrated testnet -> demo-api (Nov 2025)
SERVER_PORT = 8888

# ─────────────────────────────────────────────
# TELEGRAM ALERTS — for trade closure notifications
# (same bot token / chat ID used by aegis_ai.py)
# Leave blank to disable closure Telegram alerts.
# ─────────────────────────────────────────────
try:
    from aegis_secrets import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
except ImportError:
    TELEGRAM_BOT_TOKEN = ""
    TELEGRAM_CHAT_ID   = ""

# Safety limits — DO NOT remove these
MAX_TRADES_PER_DAY  = 10       # max trades the bot can place per day
MAX_OPEN_POSITIONS  = 3        # max simultaneous open positions
MAX_OPEN_POSITIONS_PER_ASSET = 1  # max open positions in any single asset
                                   # prevents stacking into the same symbol
                                   # during a regime shift
DAILY_LOSS_LIMIT    = 5.0      # halt if daily loss exceeds 5% of balance
MIN_OPP_SCORE       = 68       # minimum opportunity score to place a trade
MIN_CONF_SCORE      = 65       # minimum confidence score to place a trade
MIN_VOLUME_RATIO    = 1.2      # minimum volume vs average
MAX_RISK_SCORE      = 50       # maximum risk score allowed
RISK_PER_TRADE_PCT  = 1.0      # max % of balance to risk per trade

# ─────────────────────────────────────────────
# LOG FILE PATHS — saved in same folder as this script
# ─────────────────────────────────────────────
SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG_DB    = os.path.join(SCRIPT_DIR, "aegis.db")
TRADE_LOG_CSV   = os.path.join(SCRIPT_DIR, "aegis_trades.csv")   # backup export only
SIGNAL_LOG_CSV  = os.path.join(SCRIPT_DIR, "aegis_signals.csv")
EVENT_LOG_TXT   = os.path.join(SCRIPT_DIR, "aegis_events.txt")

TRADE_FIELDNAMES = [
    "date", "time", "symbol", "side", "qty",
    "entry", "stop", "target", "exit_price",
    "pnl_usdt", "pnl_pct", "rr_achieved",
    "opp_score", "conf_score", "risk_score",
    "volume_ratio", "tf_bullish", "regime",
    "order_id", "status", "duration_min",
    "order_list_id", "initial_risk_usd", "exit_time", "result",
]

def _to_db_val(v):
    """Store empty strings as NULL; leave all other values as-is."""
    return None if (v == "" or v is None) else v

def init_db():
    """Create the trades table if it doesn't exist."""
    with sqlite3.connect(TRADE_LOG_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                date             TEXT,
                time             TEXT,
                symbol           TEXT,
                side             TEXT,
                qty              REAL,
                entry            REAL,
                stop             REAL,
                target           REAL,
                exit_price       REAL,
                pnl_usdt         REAL,
                pnl_pct          REAL,
                rr_achieved      REAL,
                opp_score        INTEGER,
                conf_score       INTEGER,
                risk_score       INTEGER,
                volume_ratio     REAL,
                tf_bullish       INTEGER,
                regime           TEXT,
                order_id         TEXT UNIQUE,
                status           TEXT,
                duration_min     REAL,
                order_list_id    TEXT,
                initial_risk_usd REAL,
                exit_time        TEXT,
                result           TEXT
            )
        """)

def migrate_csv_to_db():
    """One-time import of aegis_trades.csv into aegis.db. Skips if DB already has rows."""
    if not os.path.isfile(TRADE_LOG_CSV):
        return
    try:
        with sqlite3.connect(TRADE_LOG_DB) as conn:
            if conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] > 0:
                return
        with open(TRADE_LOG_CSV, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return
        with sqlite3.connect(TRADE_LOG_DB) as conn:
            for row in rows:
                conn.execute(
                    f"INSERT OR IGNORE INTO trades ({','.join(TRADE_FIELDNAMES)}) "
                    f"VALUES ({','.join(['?'] * len(TRADE_FIELDNAMES))})",
                    [_to_db_val(row.get(k, "")) for k in TRADE_FIELDNAMES]
                )
        log(f"Migrated {len(rows)} row(s) from aegis_trades.csv to aegis.db", "OK")
    except Exception as e:
        log(f"CSV→DB migration failed: {e}", "WARN")

def export_trades_csv():
    """Write all rows from DB to aegis_trades.csv as a read-only backup."""
    try:
        with sqlite3.connect(TRADE_LOG_DB) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT {','.join(TRADE_FIELDNAMES)} FROM trades ORDER BY id"
            ).fetchall()
        with open(TRADE_LOG_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_FIELDNAMES)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: (row[k] if row[k] is not None else "") for k in TRADE_FIELDNAMES})
    except Exception as e:
        log(f"CSV backup export failed: {e}", "WARN")

# ─────────────────────────────────────────────
# STATE TRACKING
# ─────────────────────────────────────────────
state = {
    "trades_today": 0,
    "open_positions": [],
    "daily_pnl": 0.0,
    "circuit_breaker": False,
    "trade_log": [],
    "start_time": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    "last_signal": None
}

def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%H:%M:%S")
    prefix = {"INFO": "›", "OK": "✓", "WARN": "⚠", "ERR": "✗", "TRADE": "◈"}.get(level, "›")
    line = f"[{ts}] {prefix} {msg}"
    # encode/decode with 'replace' so Windows CP1252 terminals never crash
    # on Unicode symbols (✓ ⚠ etc.) — they become '?' in worst case but
    # never raise UnicodeEncodeError
    try:
        print(line)
    except UnicodeEncodeError:
        safe = line.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8", errors="replace")
        print(safe)
    state["trade_log"].append({"ts": ts, "level": level, "msg": msg})
    write_event(msg, level)
    if len(state["trade_log"]) > 500:
        state["trade_log"] = state["trade_log"][-500:]

def write_event(msg, level="INFO"):
    """Persist every log line to aegis_events.txt"""
    try:
        with open(EVENT_LOG_TXT, "a", encoding="utf-8") as f:
            ts = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
            f.write("[" + ts + "] [" + level + "] " + str(msg) + "\n")
    except Exception as e:
        print("Event log write failed: " + str(e))

def write_trade_csv(trade: dict):
    """Insert a trade into aegis.db and refresh the CSV backup."""
    try:
        with sqlite3.connect(TRADE_LOG_DB) as conn:
            conn.execute(
                f"INSERT OR IGNORE INTO trades ({','.join(TRADE_FIELDNAMES)}) "
                f"VALUES ({','.join(['?'] * len(TRADE_FIELDNAMES))})",
                [_to_db_val(trade.get(k, "")) for k in TRADE_FIELDNAMES]
            )
        log(f"Trade written to DB: {trade.get('order_id', '?')}", "OK")
    except Exception as e:
        log(f"DB write failed: {e}", "ERR")
    export_trades_csv()

def write_signal_csv(signal: dict, verdict: str, reasons: list):
    """Log every signal scan result to aegis_signals.csv"""
    fieldnames = [
        "datetime","symbol","price","pct_24h",
        "opp_score","conf_score","risk_score",
        "volume_ratio","tf_bullish","regime","verdict","reasons"
    ]
    file_exists = os.path.isfile(SIGNAL_LOG_CSV)
    try:
        with open(SIGNAL_LOG_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                "datetime":     datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S"),
                "symbol":       signal.get("symbol",""),
                "price":        signal.get("price",""),
                "pct_24h":      signal.get("pct_24h",""),
                "opp_score":    signal.get("opp_score",""),
                "conf_score":   signal.get("conf_score",""),
                "risk_score":   signal.get("risk_score",""),
                "volume_ratio": signal.get("volume_ratio",""),
                "tf_bullish":   signal.get("tf_bullish",""),
                "regime":       signal.get("regime",""),
                "verdict":      verdict,
                "reasons":      " | ".join(reasons)
            })
    except Exception as e:
        log(f"Signal log write failed: {e}", "ERR")

# ─────────────────────────────────────────────
# BINANCE SIGNING
# ─────────────────────────────────────────────
def sign(params: dict) -> str:
    query = urllib.parse.urlencode(params)
    signature = hmac.new(
        SECRET_KEY.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return query + "&signature=" + signature

def binance_get(path, params=None, signed=False):
    if params is None:
        params = {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        query = sign(params)
    else:
        query = urllib.parse.urlencode(params)
    url = f"{REST_BASE}{path}?{query}" if query else f"{REST_BASE}{path}"
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        log(f"Binance error {e.code}: {body}", "ERR")
        return {"error": body, "code": e.code}
    except Exception as e:
        log(f"Request failed: {e}", "ERR")
        return {"error": str(e)}

def binance_post(path, params):
    params["timestamp"] = int(time.time() * 1000)
    query = sign(params)
    url = f"{REST_BASE}{path}"
    data = query.encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"X-MBX-APIKEY": API_KEY,
                 "Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        log(f"Binance POST error {e.code}: {body}", "ERR")
        return {"error": body, "code": e.code}
    except Exception as e:
        log(f"POST failed: {e}", "ERR")
        return {"error": str(e)}

# ─────────────────────────────────────────────
# SAFETY CIRCUIT BREAKER
# ─────────────────────────────────────────────
def check_circuit_breaker(reason=""):
    if state["circuit_breaker"]:
        return True, "Circuit breaker already active"
    if state["trades_today"] >= MAX_TRADES_PER_DAY:
        state["circuit_breaker"] = True
        msg = f"CIRCUIT BREAKER: Max trades/day ({MAX_TRADES_PER_DAY}) reached"
        log(msg, "WARN")
        return True, msg
    if len(state["open_positions"]) >= MAX_OPEN_POSITIONS:
        return True, f"Max open positions ({MAX_OPEN_POSITIONS}) reached"
    if state["daily_pnl"] <= -DAILY_LOSS_LIMIT:
        state["circuit_breaker"] = True
        msg = f"CIRCUIT BREAKER: Daily loss limit ({DAILY_LOSS_LIMIT}%) breached"
        log(msg, "WARN")
        return True, msg
    return False, "OK"

# ─────────────────────────────────────────────
# SIGNAL VALIDATION
# ─────────────────────────────────────────────
def validate_signal(signal: dict):
    errors = []
    opp  = signal.get("opp_score", 0)
    conf = signal.get("conf_score", 0)
    risk = signal.get("risk_score", 100)
    vol  = signal.get("volume_ratio", 0)
    tf   = signal.get("tf_bullish", 0)
    sym  = signal.get("symbol", "")

    if opp  < MIN_OPP_SCORE:   errors.append(f"Opp score {opp} < {MIN_OPP_SCORE}")
    if conf < MIN_CONF_SCORE:   errors.append(f"Conf score {conf} < {MIN_CONF_SCORE}")
    if risk > MAX_RISK_SCORE:   errors.append(f"Risk score {risk} > {MAX_RISK_SCORE}")
    if vol  < MIN_VOLUME_RATIO: errors.append(f"Volume ratio {vol} < {MIN_VOLUME_RATIO}")
    if tf   < 2:                errors.append(f"Only {tf}/3 TF bullish (need 2+)")

    # ── Per-asset concentration check ──
    # Prevents stacking multiple positions into the same symbol.
    # Catches the pattern where two signals fire within one scan cycle
    # on the same asset (e.g. both entered ETHUSDT 16 min apart today).
    if sym:
        open_in_asset = sum(
            1 for p in state["open_positions"]
            if p.get("symbol") == sym
        )
        if open_in_asset >= MAX_OPEN_POSITIONS_PER_ASSET:
            errors.append(
                f"Already {open_in_asset} open position(s) in {sym} "
                f"(max {MAX_OPEN_POSITIONS_PER_ASSET} per asset)"
            )

    tripped, reason = check_circuit_breaker()
    if tripped:
        errors.append(reason)

    return len(errors) == 0, errors

# ─────────────────────────────────────────────
# POSITION SIZING
# ─────────────────────────────────────────────
# Maximum fraction of account balance that can be spent on a single
# position, regardless of what position sizing math produces. This is
# a hard backstop against bugs in entry_price/stop_price (e.g. a
# symbol/price mismatch causing price_risk to be tiny relative to price).
MAX_POSITION_VALUE_PCT = 5.0   # max 5% of balance per position

# ─────────────────────────────────────────────
# SYMBOL PRECISION (LOT_SIZE) — fetched once, cached
# ─────────────────────────────────────────────
_symbol_filters_cache = {}

def get_symbol_filters(symbol):
    """
    Fetches and caches LOT_SIZE (quantity) and PRICE_FILTER (price)
    precision for a symbol from Binance exchangeInfo.
    Returns dict: {"lot_step": float, "lot_step_str": str,
                    "tick_size": float, "tick_size_str": str}
    Falls back to sensible defaults if unavailable.
    """
    if symbol in _symbol_filters_cache:
        return _symbol_filters_cache[symbol]

    defaults = {
        "lot_step": 0.00001, "lot_step_str": "0.00001000",
        "tick_size": 0.01,   "tick_size_str": "0.01000000",
    }

    result = binance_get("/exchangeInfo", {"symbol": symbol})
    if "error" in result:
        log(f"Could not fetch exchangeInfo for {symbol}: {result['error']}", "WARN")
        return defaults

    try:
        symbols = result.get("symbols", [])
        if not symbols:
            return defaults
        filters = symbols[0].get("filters", [])
        out = dict(defaults)
        for f in filters:
            if f.get("filterType") == "LOT_SIZE":
                out["lot_step_str"] = f.get("stepSize", defaults["lot_step_str"])
                out["lot_step"] = float(out["lot_step_str"])
            elif f.get("filterType") == "PRICE_FILTER":
                out["tick_size_str"] = f.get("tickSize", defaults["tick_size_str"])
                out["tick_size"] = float(out["tick_size_str"])
        _symbol_filters_cache[symbol] = out
        log(f"Cached filters for {symbol}: lot_step={out['lot_step_str']} "
            f"tick_size={out['tick_size_str']}", "INFO")
        return out
    except Exception as e:
        log(f"Error parsing exchangeInfo for {symbol}: {e}", "WARN")

    return defaults


def get_lot_size_step(symbol):
    """Backward-compat wrapper. Returns (step_float, step_str)."""
    f = get_symbol_filters(symbol)
    return (f["lot_step"], f["lot_step_str"])

def round_to_step(qty, step, step_str=None):
    """
    Rounds qty DOWN to the nearest valid step size increment.
    Uses the original string representation of step (e.g. "0.01000000"
    from Binance) to determine decimal precision reliably -- avoids
    floating point representation issues with values like 0.00001
    (which Python renders as "1e-05").
    """
    if step <= 0:
        return qty

    # Determine decimal precision from the step's string form
    if step_str:
        s = step_str.rstrip('0')
    else:
        s = f"{step:.10f}".rstrip('0')

    if '.' in s:
        precision = len(s.split('.')[1])
    else:
        precision = 0

    # Round DOWN to nearest step (never round up -- avoid overspending)
    steps = int(qty / step + 1e-9)  # small epsilon guards against float error
    result = steps * step
    return round(result, precision)


def calc_position_size(symbol, entry_price, stop_price):
    account = binance_get("/account", signed=True)
    if "error" in account:
        return None, "Could not fetch account balance"
    balances = {b["asset"]: float(b["free"]) for b in account.get("balances", [])}
    usdt_balance = balances.get("USDT", 0)
    if usdt_balance < 10:
        return None, f"Insufficient USDT balance: {usdt_balance}"

    if entry_price <= 0:
        return None, f"Invalid entry_price: {entry_price}"

    risk_amount = usdt_balance * (RISK_PER_TRADE_PCT / 100)
    price_risk  = abs(entry_price - stop_price)
    if price_risk == 0:
        return None, "Stop price equals entry price"

    qty = risk_amount / price_risk

    # HARD BACKSTOP: cap total position value regardless of risk math.
    # If price_risk is abnormally small relative to entry_price (which
    # happens if entry/stop prices come from mismatched data), qty could
    # be huge. Cap position value to MAX_POSITION_VALUE_PCT of balance.
    position_value = qty * entry_price
    max_position_value = usdt_balance * (MAX_POSITION_VALUE_PCT / 100)
    if position_value > max_position_value:
        log(f"Position value ${position_value:.2f} exceeds cap of "
            f"${max_position_value:.2f} ({MAX_POSITION_VALUE_PCT}% of balance). "
            f"Capping quantity.", "WARN")
        qty = max_position_value / entry_price

    # Round to the symbol's actual LOT_SIZE step (e.g. SOL might need
    # 0.01 increments, not 5 decimal places)
    step, step_str = get_lot_size_step(symbol)
    qty = round_to_step(qty, step, step_str)

    if qty <= 0:
        return None, f"Calculated quantity is zero or negative after LOT_SIZE rounding: {qty} (step={step_str})"

    log(f"Position size: {qty} {symbol} | Risk: ${risk_amount:.2f} | "
        f"Value: ${qty*entry_price:.2f} | Balance: ${usdt_balance:.2f}", "INFO")
    return qty, None

# ─────────────────────────────────────────────
# TRADE EXECUTION
# ─────────────────────────────────────────────
def place_trade(signal: dict):
    symbol     = signal.get("symbol", "BTCUSDT")
    side       = signal.get("side", "BUY")
    entry      = float(signal.get("entry_price", 0))
    stop       = float(signal.get("stop_price", 0))
    target     = float(signal.get("target_price", 0))
    order_type = signal.get("order_type", "MARKET")

    # Safety check
    valid, errors = validate_signal(signal)
    if not valid:
        log(f"Signal rejected: {'; '.join(errors)}", "WARN")
        return {"status": "rejected", "reasons": errors}

    # Position sizing
    qty, err = calc_position_size(symbol, entry, stop)
    if err:
        log(f"Position sizing failed: {err}", "ERR")
        return {"status": "error", "message": err}

    # Place main order
    log(f"Placing {side} {order_type} order: {qty} {symbol}", "TRADE")
    order_params = {
        "symbol":    symbol,
        "side":      side,
        "type":      order_type,
        "quantity":  qty,
        "newOrderRespType": "FULL"
    }
    if order_type == "LIMIT":
        order_params["price"] = str(entry)
        order_params["timeInForce"] = "GTC"

    result = binance_post("/order", order_params)
    if "error" in result:
        log(f"Order failed: {result['error']}", "ERR")
        return {"status": "error", "message": result["error"]}

    order_id  = result.get("orderId")
    fill_price = float(result.get("fills", [{}])[0].get("price", entry) if result.get("fills") else entry)
    log(f"Order placed: ID {order_id} | Fill: ${fill_price}", "OK")

    # ── Compute NET quantity actually held after fees ──
    # executedQty is the gross amount bought. If the trading fee was
    # deducted in the BASE asset (e.g. ETH fee on an ETHUSDT buy --
    # happens whenever BNB fee discount isn't active), the wallet
    # ends up holding LESS than executedQty. Placing SL/TP for the
    # gross executedQty then fails with -2010 insufficient balance,
    # because Binance correctly rejects selling more than is held.
    #
    # Fix: sum commission from fills[] where commissionAsset matches
    # the base asset (symbol minus its quote suffix), subtract from
    # executedQty, then floor to lot_step so float-precision drift
    # never causes the SL/TP qty to exceed actual holdings.
    executed_qty = float(result.get("executedQty", qty))

    base_asset = symbol
    for quote in ("USDT", "USDC", "BUSD", "FDUSD", "BTC", "ETH"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            base_asset = symbol[: -len(quote)]
            break

    base_asset_fee = sum(
        float(f.get("commission", 0))
        for f in result.get("fills", [])
        if f.get("commissionAsset") == base_asset
    )

    net_qty = executed_qty - base_asset_fee
    lot_step, lot_step_str = get_lot_size_step(symbol)
    net_qty = round_to_step(net_qty, lot_step, lot_step_str)

    if base_asset_fee > 0:
        log(f"Fee deducted in {base_asset}: {base_asset_fee} | "
            f"Executed: {executed_qty} -> Net held: {net_qty}", "INFO")

    if net_qty <= 0:
        log(f"Net quantity after fees is zero or negative ({net_qty}). "
            f"Cannot place SL/TP.", "ERR")
        net_qty = executed_qty  # fall through to attempted (will likely
                                 # fail -2010, but keeps existing
                                 # unprotected-position handling intact)

    # ── Place SL + TP as a single OCO order ──
    # OCO (One-Cancels-the-Other) places both orders as a LINKED PAIR
    # against ONE reserved quantity, eliminating the balance-lock conflict
    # that caused two independent orders to fail (-2010 insufficient
    # balance on the second order).
    #
    # New endpoint (post Aug-2025 migration): POST /api/v3/orderList/oco
    #   - "aboveType"/"belowType" replace the old single "type" param
    #   - For a SELL-side OCO closing a BUY position:
    #       ABOVE leg (take-profit) = LIMIT_MAKER at target price
    #       BELOW leg (stop-loss)   = STOP_LOSS_LIMIT at stop price
    exit_side = "SELL" if side == "BUY" else "BUY"

    # Round all prices to the symbol's PRICE_FILTER tick size to avoid
    # -1013 PRICE_FILTER rejections (same precision issue as LOT_SIZE,
    # but for price instead of quantity).
    filt = get_symbol_filters(symbol)
    tick, tick_str = filt["tick_size"], filt["tick_size_str"]

    target_r      = round_to_step(target, tick, tick_str)
    stop_r        = round_to_step(stop, tick, tick_str)
    sl_limit_r    = round_to_step(stop * 0.999, tick, tick_str)

    oco_params = {
        "symbol":          symbol,
        "side":            exit_side,
        "quantity":        net_qty,
        # Above leg = take-profit (LIMIT_MAKER, no stop trigger needed)
        "aboveType":       "LIMIT_MAKER",
        "abovePrice":      str(target_r),
        # Below leg = stop-loss (STOP_LOSS_LIMIT, triggers at stopPrice)
        "belowType":       "STOP_LOSS_LIMIT",
        "belowStopPrice":  str(stop_r),
        "belowPrice":      str(sl_limit_r),
        "belowTimeInForce": "GTC",
        "newOrderRespType": "FULL",
    }

    oco_result = binance_post("/orderList/oco", oco_params)
    oco_ok = "error" not in oco_result

    if oco_ok:
        log(f"OCO placed: TP ${target} / SL ${stop} (linked pair, "
            f"orderListId={oco_result.get('orderListId')})", "OK")
        sl_ok = True
        tp_ok = True
    else:
        log(f"OCO order failed: {oco_result.get('error')}", "ERR")
        sl_ok = False
        tp_ok = False

        # FALLBACK: if OCO fails (e.g. endpoint unavailable on this
        # demo platform version), fall back to a STOP-LOSS-ONLY order.
        # Downside protection matters more than upside target -- a
        # missing take-profit is recoverable, an unprotected position
        # on the downside is not.
        log("OCO failed -- falling back to stop-loss-only order", "WARN")
        sl_result = binance_post("/order", {
            "symbol":    symbol,
            "side":      exit_side,
            "type":      "STOP_LOSS_LIMIT",
            "quantity":  net_qty,
            "stopPrice": str(stop_r),
            "price":     str(sl_limit_r),
            "timeInForce": "GTC"
        })
        sl_ok = "error" not in sl_result
        log(f"Fallback stop-loss placed at ${stop}", "OK" if sl_ok else "ERR")
        tp_ok = False  # no TP in fallback mode

    # CRITICAL: if the position ended up without a stop-loss (the one
    # protection that matters most), flag it loudly and halt the bot.
    if not sl_ok:
        unprotected_msg = (
            f"UNPROTECTED POSITION: {symbol} {side} {net_qty} filled at ${fill_price} "
            f"but STOP-LOSS FAILED (OCO and fallback both failed). "
            f"MANUAL INTERVENTION REQUIRED on demo.binance.com."
        )
        log(unprotected_msg, "WARN")
        write_event(unprotected_msg, "ERR")
        state["circuit_breaker"] = True
        log("CIRCUIT BREAKER TRIPPED: unprotected position detected. "
            "Trading halted until /reset after manual review.", "WARN")
    elif not tp_ok:
        # SL is active but no TP -- not ideal but not dangerous. Log it
        # clearly but do NOT trip the circuit breaker for this alone.
        degraded_msg = (
            f"DEGRADED PROTECTION: {symbol} {side} {net_qty} filled at ${fill_price} -- "
            f"stop-loss active at ${stop}, but no take-profit placed. "
            f"Position will rely on manual exit or future code update for profit-taking."
        )
        log(degraded_msg, "WARN")
        write_event(degraded_msg, "WARN")

    # Record trade
    order_list_id_val = oco_result.get("orderListId", "") if oco_ok else ""
    initial_risk_usd  = round(abs(fill_price - stop) * net_qty, 4)

    trade_record = {
        "id":              order_id,
        "symbol":          symbol,
        "side":            side,
        "qty":             net_qty,
        "qty_requested":   qty,
        "entry":           fill_price,
        "stop":            stop,
        "target":          target,
        "sl_active":       sl_ok,
        "tp_active":       tp_ok,
        "opp_score":       signal.get("opp_score"),
        "conf_score":      signal.get("conf_score"),
        "risk_score":      signal.get("risk_score"),
        "time":            datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "order_list_id":   order_list_id_val,
        "initial_risk_usd": initial_risk_usd,
        "status":          ("open" if (sl_ok and tp_ok)
                            else "UNPROTECTED" if not sl_ok
                            else "DEGRADED")
    }
    state["open_positions"].append(trade_record)
    state["trades_today"] += 1
    write_trade_csv({
        **trade_record,
        "date":            datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d"),
        "time":            datetime.now(timezone.utc).replace(tzinfo=None).strftime("%H:%M:%S"),
        "exit_price":      "", "pnl_usdt": "", "pnl_pct": "",
        "rr_achieved":     "", "duration_min": "",
        "exit_time":       "", "result": "",
        "order_list_id":   order_list_id_val,
        "initial_risk_usd": initial_risk_usd,
        "status":          "open"
    })
    write_event(f"TRADE OPENED: {symbol} {side} {qty} @ {fill_price} | SL:{stop} TP:{target}", "TRADE")

    rr = round(abs(target - fill_price) / abs(fill_price - stop), 2) if stop != fill_price else 0
    log(f"Trade logged | R:R {rr}:1 | Trades today: {state['trades_today']}", "TRADE")

    return {"status": "success", "order": trade_record, "rr": rr}

# ─────────────────────────────────────────────
# TELEGRAM HELPER (server-side, for closure alerts)
# ─────────────────────────────────────────────
def send_telegram(message: str):
    """Send a Telegram message. Silently skips if token/chat_id not configured."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
    except Exception as e:
        log(f"Telegram send failed: {e}", "WARN")


# ─────────────────────────────────────────────
# P1 — TRADE CLOSURE / RECONCILIATION ENGINE
# ─────────────────────────────────────────────
def rewrite_trade_csv_row(order_id: str, updates: dict) -> bool:
    """Update a trade row in aegis.db by order_id, then refresh the CSV backup."""
    if not updates:
        return False
    try:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = [_to_db_val(v) for v in updates.values()] + [str(order_id)]
        with sqlite3.connect(TRADE_LOG_DB) as conn:
            cur = conn.execute(
                f"UPDATE trades SET {set_clause} WHERE order_id = ?", values
            )
            if cur.rowcount == 0:
                log(f"rewrite_trade_csv_row: order_id {order_id} not found in DB", "WARN")
                return False
        export_trades_csv()
        return True
    except Exception as e:
        log(f"DB row update failed: {e}", "ERR")
        return False


def get_open_trade_rows() -> list:
    """Return trades with status 'open' or 'degraded' from aegis.db."""
    try:
        with sqlite3.connect(TRADE_LOG_DB) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trades WHERE LOWER(status) IN ('open', 'degraded')"
            ).fetchall()
            # Return None as "" to match the CSV-reader interface expected by callers
            return [{k: (row[k] if row[k] is not None else "") for k in row.keys()} for row in rows]
    except Exception as e:
        log(f"DB read error in get_open_trade_rows: {e}", "ERR")
        return []


def check_open_trades() -> list:
    """
    Poll Binance for the status of every open/DEGRADED trade row in
    aegis_trades.csv. Detect fills, write closure data back to CSV,
    update state["open_positions"], and send Telegram close summaries.

    Returns a list of closure dicts for any trades closed this call.
    """
    open_rows = get_open_trade_rows()
    if not open_rows:
        return []

    closures = []

    # Compute running stats for Telegram summary context
    all_closed = []
    try:
        with sqlite3.connect(TRADE_LOG_DB) as conn:
            conn.row_factory = sqlite3.Row
            all_closed = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM trades WHERE status LIKE 'closed%'"
                ).fetchall()
            ]
    except Exception:
        pass

    for row in open_rows:
        order_id     = row.get("order_id", "")
        symbol       = row.get("symbol", "")
        order_list_id = row.get("order_list_id", "").strip()
        status       = row.get("status", "").lower()

        if not order_id or not symbol:
            continue

        exit_price   = None
        exit_time_str = None
        closed_via   = None  # "TP", "SL", or "manual"

        # ── Path A: OCO trade (has order_list_id) ──
        if order_list_id and status == "open":
            oco_info = binance_get(
                "/orderList",
                {"orderListId": order_list_id},
                signed=True
            )
            if "error" in oco_info:
                log(f"OCO status check failed for orderListId {order_list_id}: "
                    f"{oco_info.get('error')}", "WARN")
                continue

            list_status = oco_info.get("listOrderStatus", "")
            if list_status != "ALL_DONE":
                # OCO still active — both legs open, nothing to close
                continue

            # One leg filled, the other was auto-cancelled — find which
            orders_in_list = oco_info.get("orders", [])
            for leg in orders_in_list:
                leg_id = leg.get("orderId")
                leg_detail = binance_get(
                    "/order",
                    {"symbol": symbol, "orderId": leg_id},
                    signed=True
                )
                if leg_detail.get("status") == "FILLED":
                    exit_price = float(leg_detail.get("price") or
                                       leg_detail.get("stopPrice") or 0)
                    # For LIMIT_MAKER fills, use avgPrice if available
                    if leg_detail.get("avgPrice") and float(leg_detail["avgPrice"]) > 0:
                        exit_price = float(leg_detail["avgPrice"])
                    exit_time_str = leg_detail.get("updateTime")
                    if exit_time_str:
                        exit_time_str = datetime.fromtimestamp(
                            int(exit_time_str) / 1000, tz=timezone.utc
                        ).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
                    # Determine which leg filled: TP is above entry (LIMIT_MAKER),
                    # SL is below entry (STOP_LOSS_LIMIT)
                    entry_px = float(row.get("entry", 0))
                    closed_via = "TP" if exit_price > entry_px else "SL"
                    break

        # ── Path B: DEGRADED trade (SL-only, no OCO) ──
        elif status == "degraded":
            # Find the open SL order for this symbol
            open_orders = binance_get("/openOrders", {"symbol": symbol}, signed=True)
            if isinstance(open_orders, dict) and "error" in open_orders:
                log(f"Could not fetch open orders for {symbol}: "
                    f"{open_orders.get('error')}", "WARN")
                continue

            # Check recent filled orders for this symbol
            filled_orders = binance_get(
                "/allOrders",
                {"symbol": symbol, "limit": 10},
                signed=True
            )
            if isinstance(filled_orders, dict) and "error" in filled_orders:
                continue

            # Find the most recent SELL FILLED order after the entry time
            entry_time_str = f"{row.get('date','')} {row.get('time','')}"
            try:
                entry_dt = datetime.strptime(entry_time_str, "%Y-%m-%d %H:%M:%S")
                entry_ts_ms = int(entry_dt.timestamp() * 1000)
            except Exception:
                entry_ts_ms = 0

            for o in (filled_orders if isinstance(filled_orders, list) else []):
                if (o.get("side") == "SELL"
                        and o.get("status") == "FILLED"
                        and int(o.get("updateTime", 0)) > entry_ts_ms):
                    exit_price = float(o.get("price") or o.get("stopPrice") or 0)
                    if o.get("avgPrice") and float(o["avgPrice"]) > 0:
                        exit_price = float(o["avgPrice"])
                    exit_time_str = datetime.fromtimestamp(
                        int(o["updateTime"]) / 1000, tz=timezone.utc
                    ).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
                    closed_via = "SL"
                    break

        # ── No fill detected — skip ──
        if exit_price is None or exit_price <= 0:
            # Extra check: flag DEGRADED trades where price has crossed
            # original TP without a TP order existing
            if status == "degraded":
                target_px = float(row.get("target", 0))
                entry_px  = float(row.get("entry", 0))
                if target_px > 0 and entry_px > 0:
                    # Fetch current price
                    ticker = binance_get("/ticker/price", {"symbol": symbol})
                    current_px = float(ticker.get("price", 0))
                    if current_px >= target_px:
                        log(f"⚠ DEGRADED {symbol}: current price ${current_px:.2f} "
                            f"has reached/exceeded TP target ${target_px:.2f} "
                            f"but NO TP order exists. Manual review recommended.", "WARN")
            continue

        # ── Fill detected — compute closure metrics ──
        entry_px     = float(row.get("entry", 0))
        qty_held     = float(row.get("qty", 0))
        stop_px      = float(row.get("stop", 0))
        entry_date   = row.get("date", "")
        entry_time   = row.get("time", "")

        # PnL (long positions: buy low sell high)
        pnl_usd  = round((exit_price - entry_px) * qty_held, 4)
        pnl_pct  = round((pnl_usd / (entry_px * qty_held)) * 100, 3) if entry_px * qty_held > 0 else 0

        # R-achieved: use initial_risk_usd if recorded, else compute from SL distance
        initial_risk_usd = float(row.get("initial_risk_usd", 0))
        if initial_risk_usd <= 0 and stop_px > 0 and entry_px > 0:
            initial_risk_usd = abs(entry_px - stop_px) * qty_held
        r_achieved = round(pnl_usd / initial_risk_usd, 3) if initial_risk_usd > 0 else 0

        # Duration
        duration_min = ""
        if exit_time_str and entry_date and entry_time:
            try:
                entry_dt = datetime.strptime(f"{entry_date} {entry_time}", "%Y-%m-%d %H:%M:%S")
                exit_dt  = datetime.strptime(exit_time_str, "%Y-%m-%d %H:%M:%S")
                duration_min = round((exit_dt - entry_dt).total_seconds() / 60, 1)
            except Exception:
                pass

        result_str = "WIN" if pnl_usd > 0 else "LOSS" if pnl_usd < 0 else "BREAKEVEN"

        closure = {
            "order_id":    order_id,
            "symbol":      symbol,
            "side":        row.get("side", "BUY"),
            "entry":       entry_px,
            "exit_price":  exit_price,
            "exit_time":   exit_time_str or "",
            "pnl_usdt":    pnl_usd,
            "pnl_pct":     pnl_pct,
            "rr_achieved": r_achieved,
            "duration_min": duration_min,
            "result":      result_str,
            "closed_via":  closed_via,
            "status":      f"closed_{closed_via.lower()}" if closed_via else "closed",
        }
        closures.append(closure)

        # ── Rewrite CSV row ──
        updated = rewrite_trade_csv_row(order_id, {
            "exit_price":   exit_price,
            "exit_time":    exit_time_str or "",
            "pnl_usdt":     pnl_usd,
            "pnl_pct":      pnl_pct,
            "rr_achieved":  r_achieved,
            "duration_min": duration_min,
            "result":       result_str,
            "status":       closure["status"],
        })

        if updated:
            log(f"Trade closed: {symbol} {closed_via} | "
                f"Entry ${entry_px} → Exit ${exit_price:.2f} | "
                f"PnL ${pnl_usd:+.2f} ({pnl_pct:+.2f}%) | "
                f"{r_achieved:+.3f}R | {result_str}", "TRADE")
        else:
            log(f"CSV rewrite failed for order_id {order_id} -- closure not persisted", "ERR")

        # ── Remove from in-memory open_positions ──
        state["open_positions"] = [
            p for p in state["open_positions"]
            if str(p.get("id", "")) != str(order_id)
        ]

        # ── Update daily PnL ──
        state["daily_pnl"] = round(state["daily_pnl"] + pnl_usd, 4)

        # ── Compute running win rate for Telegram ──
        all_results = [r.get("result", "") for r in all_closed] + [result_str]
        wins   = sum(1 for r in all_results if r == "WIN")
        losses = sum(1 for r in all_results if r == "LOSS")
        total  = wins + losses
        win_rate_str = f"{wins}/{total} ({100*wins//total}%)" if total > 0 else "N/A"

        # ── Send Telegram close summary ──
        emoji = "🟢" if result_str == "WIN" else "🔴" if result_str == "LOSS" else "⚪"
        msg = (
            f"{emoji} <b>AEGIS TRADE CLOSED — {result_str}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Asset:</b> {symbol}  |  <b>Side:</b> {row.get('side','BUY')}\n"
            f"<b>Exit via:</b> {closed_via or 'unknown'}\n"
            f"<b>Entry:</b> ${entry_px:.4f}  →  <b>Exit:</b> ${exit_price:.4f}\n"
            f"<b>PnL:</b> ${pnl_usd:+.2f} ({pnl_pct:+.2f}%)\n"
            f"<b>R-achieved:</b> {r_achieved:+.3f}R\n"
            f"<b>Duration:</b> {duration_min} min\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Win rate:</b> {win_rate_str}  |  "
            f"<b>Daily PnL:</b> ${state['daily_pnl']:+.2f}"
        )
        send_telegram(msg)

    return closures


# ─────────────────────────────────────────────
# HTTP REQUEST HANDLER
# ─────────────────────────────────────────────
class AegisHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress default HTTP logs

    def send_json(self, data, code=200):
        try:
            body = json.dumps(data, indent=2).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # Browser closed connection — server continues normally
        except Exception as e:
            pass  # Any other write error — absorb silently

    def handle_error(self, request, client_address):
        """Suppress noisy Windows connection reset errors (WinError 10053/10054)"""
        import sys
        exc_type, exc_val, _ = sys.exc_info()
        if exc_type and issubclass(exc_type, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return  # silent — browser closed connection early, server is fine
        # For all other errors, log them
        log(f"Request error from {client_address}: {exc_val}", "WARN")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        # ── Status ──
        if path == "/status":
            tripped, reason = check_circuit_breaker()
            self.send_json({
                "status":           "running",
                "circuit_breaker":  state["circuit_breaker"],
                "cb_reason":        reason,
                "trades_today":     state["trades_today"],
                "open_positions":   len(state["open_positions"]),
                "daily_pnl":        state["daily_pnl"],
                "uptime_since":     state["start_time"],
                "last_signal":      state["last_signal"]
            })

        # ── Account balance ──
        elif path == "/balance":
            log("Fetching account balance", "INFO")
            result = binance_get("/account", signed=True)
            if "error" in result:
                self.send_json(result, 500)
                return
            balances = [b for b in result.get("balances", []) if float(b["free"]) > 0 or float(b["locked"]) > 0]
            self.send_json({"balances": balances, "canTrade": result.get("canTrade")})

        # ── Open orders ──
        elif path == "/orders":
            result = binance_get("/openOrders", signed=True)
            self.send_json(result if not isinstance(result, dict) or "error" not in result else result)

        # ── Trade history ──
        elif path == "/trades":
            self.send_json({"trades": state["trade_log"][-50:], "total": len(state["trade_log"])})

        # ── Open positions ──
        elif path == "/positions":
            self.send_json({"positions": state["open_positions"], "count": len(state["open_positions"])})

        # ── Reset circuit breaker (manual override) ──
        elif path == "/reset":
            state["circuit_breaker"] = False
            state["trades_today"] = 0
            log("Circuit breaker reset by user", "WARN")
            self.send_json({"status": "reset", "message": "Circuit breaker cleared. Trades today reset to 0."})

        # ── Ping ──
        elif path == "/ping":
            self.send_json({"status": "ok", "server": "Aegis", "port": SERVER_PORT})

        # ── Trades as JSON (for journal dashboard) ──
        elif path == "/tradesdata":
            try:
                with sqlite3.connect(TRADE_LOG_DB) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = [
                        {k: (r[k] if r[k] is not None else "") for k in TRADE_FIELDNAMES}
                        for r in conn.execute(
                            f"SELECT {','.join(TRADE_FIELDNAMES)} FROM trades ORDER BY id"
                        ).fetchall()
                    ]
                self.send_json({"trades": rows, "count": len(rows)})
            except Exception as e:
                log(f"DB read error in /tradesdata: {e}", "ERR")
                self.send_json({"trades": [], "count": 0, "error": str(e)})

        # ── Signals as JSON (for journal dashboard) ──
        elif path == "/signalsdata":
            if os.path.isfile(SIGNAL_LOG_CSV):
                rows = []
                try:
                    with open(SIGNAL_LOG_CSV, "r", encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                        rows = list(reader)
                except Exception as e:
                    log("Signal CSV read error: " + str(e), "ERR")
                self.send_json({"signals": rows, "count": len(rows)})
            else:
                self.send_json({"signals": [], "count": 0, "message": "No signals yet"})

        # ── Download trade log ──
        elif path == "/tradelog":
            if os.path.isfile(TRADE_LOG_CSV):
                with open(TRADE_LOG_CSV, "r", encoding="utf-8") as f:
                    csv_data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition", "attachment; filename=aegis_trades.csv")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    self.wfile.write(csv_data.encode())
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                    pass
            else:
                self.send_json({"message": "No trades logged yet", "file": TRADE_LOG_CSV})

        # ── Download signal log ──
        elif path == "/signallog":
            if os.path.isfile(SIGNAL_LOG_CSV):
                with open(SIGNAL_LOG_CSV, "r", encoding="utf-8") as f:
                    csv_data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition", "attachment; filename=aegis_signals.csv")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    self.wfile.write(csv_data.encode())
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                    pass
            else:
                self.send_json({"message": "No signals logged yet", "file": SIGNAL_LOG_CSV})

        # ── Live log viewer ──
        elif path == "/logs":
            try:
                n = int(params.get("n", ["100"])[0])
            except Exception:
                n = 100
            n = min(max(n, 10), 500)  # clamp: 10–500 lines

            def tail_file(filepath, lines):
                if not os.path.isfile(filepath):
                    return []
                try:
                    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                        return f.readlines()[-lines:]
                except Exception as e:
                    return [f"[read error: {e}]"]

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            bot_log_path = os.path.join(SCRIPT_DIR, f"aegis_bot_{today}.log")

            server_lines = tail_file(EVENT_LOG_TXT, n)
            bot_lines    = tail_file(bot_log_path, n)

            self.send_json({
                "as_of":           datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                "lines_each":      n,
                "server_log":      [l.rstrip() for l in server_lines],
                "bot_log":         [l.rstrip() for l in bot_lines],
                "server_log_file": EVENT_LOG_TXT,
                "bot_log_file":    bot_log_path,
            })

        # ── Check for closed trades (P1 reconciliation) ──
        elif path == "/checkclosures":
            closures = check_open_trades()
            self.send_json({
                "checked_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                "closures_found": len(closures),
                "closures": closures
            })

        else:
            self.send_json({"error": "Unknown endpoint", "available": [
                "/status", "/balance", "/orders", "/positions", "/trades",
                "/tradelog", "/signallog", "/checkclosures", "/logs", "/reset", "/ping"
            ]}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length > 0 else {}

        # ── Place trade ──
        if path == "/trade":
            log(f"Trade signal received for {body.get('symbol','?')}", "INFO")
            state["last_signal"] = {**body, "received_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()}
            result = place_trade(body)
            code = 200 if result.get("status") == "success" else 400
            self.send_json(result, code)

        # ── Validate signal (dry run) ──
        elif path == "/validate":
            valid, errors = validate_signal(body)
            self.send_json({
                "valid":   valid,
                "errors":  errors,
                "signal":  body,
                "limits": {
                    "min_opp_score":   MIN_OPP_SCORE,
                    "min_conf_score":  MIN_CONF_SCORE,
                    "max_risk_score":  MAX_RISK_SCORE,
                    "min_volume_ratio": MIN_VOLUME_RATIO,
                    "max_trades_today": MAX_TRADES_PER_DAY
                }
            })

        # ── Log signal scan to CSV ──
        elif path == "/logsignal":
            write_signal_csv(body, body.get("verdict","WAIT"), body.get("reasons",[]))
            self.send_json({"status": "logged"})

        # ── Cancel all open orders ──
        elif path == "/cancel-all":
            symbol = body.get("symbol", "BTCUSDT")
            result = binance_post("/openOrders", {"symbol": symbol, "_method": "DELETE"})
            log(f"Cancel all orders for {symbol}", "WARN")
            self.send_json({"status": "cancel_sent", "symbol": symbol})

        # ── Update PnL ──
        elif path == "/update-pnl":
            state["daily_pnl"] = body.get("pnl", state["daily_pnl"])
            self.send_json({"daily_pnl": state["daily_pnl"]})

        else:
            self.send_json({"error": "Unknown endpoint"}, 404)

# ─────────────────────────────────────────────
# STARTUP STATE HYDRATION
# ─────────────────────────────────────────────
def hydrate_state_from_db():
    """
    On server startup, repopulate state["open_positions"] from any rows
    in aegis.db with status="open" or status="DEGRADED".

    Also restores trades_today count for the current UTC day so the
    MAX_TRADES_PER_DAY circuit breaker carries over correctly.
    """
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    open_count = 0
    today_count = 0

    try:
        with sqlite3.connect(TRADE_LOG_DB) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute("SELECT * FROM trades").fetchall()]
    except Exception as e:
        log(f"State hydration failed (non-fatal, starting clean): {e}", "WARN")
        return

    for row in rows:
        if row.get("date", "") == today_str:
            today_count += 1
        if row.get("status", "").lower() in ("open", "degraded"):
            position = {
                "id":               row.get("order_id", ""),
                "symbol":           row.get("symbol", ""),
                "side":             row.get("side", "BUY"),
                "qty":              float(row.get("qty") or 0),
                "entry":            float(row.get("entry") or 0),
                "stop":             float(row.get("stop") or 0),
                "target":           float(row.get("target") or 0),
                "status":           row.get("status", "open"),
                "order_list_id":    row.get("order_list_id") or "",
                "initial_risk_usd": float(row.get("initial_risk_usd") or 0),
                "opp_score":        int(row.get("opp_score") or 0),
                "conf_score":       int(row.get("conf_score") or 0),
                "risk_score":       int(row.get("risk_score") or 0),
                "time":             f"{row.get('date', '')} {row.get('time', '')}",
            }
            state["open_positions"].append(position)
            open_count += 1

    state["trades_today"] = today_count

    if open_count > 0 or today_count > 0:
        log(f"State hydrated from DB: {open_count} open position(s) restored, "
            f"{today_count} trade(s) today", "OK")
        for p in state["open_positions"]:
            log(f"  Restored: {p['symbol']} {p['side']} {p['qty']} @ {p['entry']} "
                f"| OCO:{p['order_list_id'] or 'none'} | {p['status']}", "INFO")
    else:
        log("State hydrated from DB: no open positions, clean slate", "OK")


# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────
def main():
    print("=" * 56)
    print("  Aegis Trading Server — Local Signing Bridge")
    print("=" * 56)
    print(f"  Port    : {SERVER_PORT}")
    print(f"  Target  : Binance Demo Testnet")
    print(f"  API Key : {API_KEY[:8]}...{API_KEY[-4:] if len(API_KEY) > 12 else '(not set)'}")
    print(f"  Safety  : Max {MAX_TRADES_PER_DAY} trades/day · {DAILY_LOSS_LIMIT}% loss limit")
    print(f"  Risk    : {RISK_PER_TRADE_PCT}% per trade · Max {MAX_OPEN_POSITIONS} positions")
    print("=" * 56)

    if API_KEY == "PASTE_YOUR_DEMO_API_KEY_HERE":
        print("\n  ⚠  WARNING: API keys not set.")
        print("     Edit aegis_server.py and paste your Demo API keys.")
        print("     Lines 21 and 22 in this file.\n")

    # Test connectivity
    try:
        result = binance_get("/time")
        if "serverTime" in result:
            print(f"\n  ✓ Binance testnet reachable")
            print(f"  ✓ Server time: {datetime.fromtimestamp(result['serverTime']/1000, tz=timezone.utc).replace(tzinfo=None).strftime('%H:%M:%S')} UTC")
        else:
            print(f"\n  ✗ Binance testnet: {result}")
    except Exception as e:
        print(f"\n  ✗ Cannot reach Binance testnet: {e}")

    print(f"\n  ✓ Aegis server running on http://localhost:{SERVER_PORT}")
    print(f"  › Endpoints: /status /balance /orders /positions /trades /validate /trade /checkclosures /logs")
    print(f"  › Stop with Ctrl+C\n")

    # Initialise DB, migrate existing CSV data, then restore live state
    init_db()
    migrate_csv_to_db()
    hydrate_state_from_db()

    server = ThreadingHTTPServer(("localhost", SERVER_PORT), AegisHandler)
    server.socket.settimeout(30)  # 30s timeout — no hung connections
    server.daemon_threads = True  # threads die with server on Ctrl+C
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\n  › Server stopped by user.\n")
        server.shutdown()

if __name__ == "__main__":
    main()
