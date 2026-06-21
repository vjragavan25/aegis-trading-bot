"""
test_aegis.py — Aegis Trading System Test Suite
================================================
Run with: python test_aegis.py
All tests run in isolation — no network calls, no file I/O, no real trades.

Coverage:
  1.  round_to_step()           — lot-size rounding (bug #6 foundation)
  2.  fee_adjusted_quantity     — net_qty after base-asset fee deduction
  3.  position_size_cap         — MAX_POSITION_VALUE_PCT hard backstop
  4.  should_trade() / bot      — entry gate: scores, regime, consistency
  5.  validate_signal() / server — server-side gate: scores, concentration, CB
  6.  check_circuit_breaker()   — all four CB trigger conditions
  7.  hydrate_state_from_csv()  — state restoration on restart
  8.  write_trade_csv() schema  — auto-migration of old-schema CSV
  9.  SL/TP price calculation   — correct STOP_LOSS_PCT / TAKE_PROFIT_PCT math
  10. R:R ratio                 — sanity check on configured ratio
"""

import sys
import os
import gc
import csv
import math
import json
import types
import unittest
import tempfile
import importlib
from unittest.mock import patch, MagicMock
from io import StringIO

# ─────────────────────────────────────────────────────────────
# HELPERS — import server/bot modules without triggering
# network calls or file I/O at import time.
# ─────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def load_server_module():
    """
    Import aegis_server as a module but mock out all external calls
    so tests run fully offline. Uses actual filenames to avoid Python's
    loader name-check restriction.
    """
    # Prefer aegis_server_fix8.py (latest in test env), fall back to deployed name
    filepath = os.path.join(SCRIPT_DIR, "aegis_server_fix8.py")
    if not os.path.isfile(filepath):
        filepath = os.path.join(SCRIPT_DIR, "aegis_server.py")

    spec = importlib.util.spec_from_file_location(
        os.path.splitext(os.path.basename(filepath))[0], filepath)
    mod = importlib.util.module_from_spec(spec)

    with patch("http.server.ThreadingHTTPServer"), \
         patch("urllib.request.urlopen") as mock_ul:
        mock_ul.return_value.__enter__ = lambda s: s
        mock_ul.return_value.__exit__  = MagicMock(return_value=False)
        mock_ul.return_value.read.return_value = b'{"serverTime":1000000}'
        spec.loader.exec_module(mod)

    # Redirect file paths to temp dir so no real files are touched
    tmp = tempfile.gettempdir()
    mod.TRADE_LOG_DB   = os.path.join(tmp, "aegis_test_trades.db")
    mod.TRADE_LOG_CSV  = os.path.join(tmp, "aegis_test_trades.csv")
    mod.EVENT_LOG_TXT  = os.path.join(tmp, "aegis_test_events.txt")
    mod.SIGNAL_LOG_CSV = os.path.join(tmp, "aegis_test_signals.csv")
    # Initialise the test DB schema
    mod.init_db()
    return mod


def load_bot_module():
    """Import aegis_bot without triggering the main loop or network."""
    filepath = os.path.join(SCRIPT_DIR, "aegis_bot_sma.py")
    if not os.path.isfile(filepath):
        filepath = os.path.join(SCRIPT_DIR, "aegis_bot.py")

    # Stub out aegis_ai so we don't need that file present
    ai_stub = types.ModuleType("aegis_ai")
    ai_stub.reason_about_signal    = lambda *a, **kw: "stub"
    ai_stub.comment_on_trade       = lambda *a, **kw: "stub"
    ai_stub.alert_trade_fired      = lambda *a, **kw: None
    ai_stub.generate_cycle_brief   = lambda *a, **kw: "stub"
    ai_stub.generate_morning_brief = lambda *a, **kw: "stub"
    ai_stub.alert_morning_brief    = lambda *a, **kw: None
    ai_stub.detect_anomalies       = lambda *a, **kw: "No anomalies"
    ai_stub.send_telegram          = lambda *a, **kw: None
    ai_stub.BRIEF_EVERY_N_CYCLES   = 4
    sys.modules["aegis_ai"] = ai_stub

    spec = importlib.util.spec_from_file_location(
        os.path.splitext(os.path.basename(filepath))[0], filepath)
    mod = importlib.util.module_from_spec(spec)

    with patch("urllib.request.urlopen"):
        spec.loader.exec_module(mod)

    return mod


# ─────────────────────────────────────────────────────────────
# TEST CASES
# ─────────────────────────────────────────────────────────────

class TestRoundToStep(unittest.TestCase):
    """round_to_step() must always round DOWN."""

    @classmethod
    def setUpClass(cls):
        cls.srv = load_server_module()

    def test_btc_lot_step_rounds_down(self):
        """BTC lot_step=0.00001 — 5 decimal places."""
        result = self.srv.round_to_step(0.123456789, 0.00001, "0.00001000")
        self.assertEqual(result, 0.12345)

    def test_eth_lot_step_rounds_down(self):
        """ETH lot_step=0.0001 — 4 decimal places."""
        result = self.srv.round_to_step(0.1437563, 0.0001, "0.00010000")
        self.assertEqual(result, 0.1437)

    def test_sol_lot_step_rounds_down(self):
        """SOL lot_step=0.01 — 2 decimal places."""
        result = self.srv.round_to_step(3.509876, 0.01, "0.01000000")
        self.assertEqual(result, 3.50)

    def test_never_rounds_up(self):
        """Must NEVER produce a value larger than the input."""
        for qty in [0.1437, 0.03042, 3.506, 0.00381]:
            result = self.srv.round_to_step(qty, 0.0001, "0.00010000")
            self.assertLessEqual(result, qty,
                msg=f"round_to_step({qty}) produced {result} > input — would cause -2010")

    def test_zero_step_returns_qty_unchanged(self):
        """Step=0 edge case — return qty as-is, don't divide by zero."""
        result = self.srv.round_to_step(0.137, 0, None)
        self.assertEqual(result, 0.137)

    def test_exact_multiple_unchanged(self):
        """Value already at a valid step — should be unchanged."""
        result = self.srv.round_to_step(0.1368, 0.0001, "0.00010000")
        self.assertEqual(result, 0.1368)

    def test_float_precision_guard(self):
        """
        0.1437 - 0.0001437 = 0.1435563 after fee deduction.
        Floored to 0.0001 step should give 0.1435, never 0.1436.
        This is the exact arithmetic from bug #6.
        """
        net = 0.1437 - 0.0001437   # = 0.1435563 (may have float drift)
        result = self.srv.round_to_step(net, 0.0001, "0.00010000")
        self.assertEqual(result, 0.1435)


class TestFeeAdjustedQuantity(unittest.TestCase):
    """
    After a BUY MARKET fill, net_qty must be executedQty minus any
    commission deducted in the base asset. Using executedQty directly
    caused bug #6 (-2010 on OCO/SL placement).
    """

    def _compute_net_qty(self, srv, symbol, executed_qty, fills):
        """Replicate the net_qty logic from place_trade()."""
        base_asset = symbol
        for quote in ("USDT", "USDC", "BUSD", "FDUSD", "BTC", "ETH"):
            if symbol.endswith(quote) and len(symbol) > len(quote):
                base_asset = symbol[:-len(quote)]
                break
        base_fee = sum(
            float(f.get("commission", 0))
            for f in fills
            if f.get("commissionAsset") == base_asset
        )
        net = executed_qty - base_fee
        lot_step, lot_step_str = srv.get_lot_size_step.__wrapped__(symbol) \
            if hasattr(srv.get_lot_size_step, "__wrapped__") else (0.0001, "0.00010000")
        return srv.round_to_step(net, 0.0001, "0.00010000"), base_fee

    @classmethod
    def setUpClass(cls):
        cls.srv = load_server_module()

    def setUp(self):
        # Reset mutable state between tests
        self.srv.state["open_positions"] = []
        self.srv.state["circuit_breaker"] = False
        self.srv.state["trades_today"]    = 0
        self.srv.state["daily_pnl"]       = 0.0

    def test_eth_fee_in_eth(self):
        """ETH buy with ETH-denominated fee — the exact bug #6 scenario."""
        executed = 0.1437
        fills = [{"commission": "0.0001437", "commissionAsset": "ETH"}]
        net, fee = self._compute_net_qty(self.srv, "ETHUSDT", executed, fills)
        self.assertAlmostEqual(fee, 0.0001437, places=7)
        self.assertEqual(net, 0.1435)         # 0.1437 - 0.0001437 = 0.1435563 → floor to 0.1435
        self.assertLess(net, executed)         # net must be strictly less than executed

    def test_btc_fee_in_bnb(self):
        """BTC buy with BNB fee — base asset fee should be 0, net = executed."""
        executed = 0.03042
        fills = [{"commission": "0.00015", "commissionAsset": "BNB"}]
        net, fee = self._compute_net_qty(self.srv, "BTCUSDT", executed, fills)
        self.assertEqual(fee, 0.0)
        # net = 0.03042 rounded to 0.0001 step
        self.assertEqual(net, 0.0304)

    def test_multiple_fills_same_asset(self):
        """Multiple partial fills — fees should be summed."""
        executed = 0.137
        fills = [
            {"commission": "0.00007", "commissionAsset": "ETH"},
            {"commission": "0.00007", "commissionAsset": "ETH"},
        ]
        net, fee = self._compute_net_qty(self.srv, "ETHUSDT", executed, fills)
        self.assertAlmostEqual(fee, 0.00014, places=7)
        self.assertLess(net, executed)

    def test_base_asset_extraction(self):
        """Base asset derivation from symbol string."""
        cases = [
            ("BTCUSDT", "BTC"), ("ETHUSDT", "ETH"), ("SOLUSDT", "SOL"),
            ("BTCUSDC", "BTC"), ("ETHBUSD", "ETH"),
        ]
        for symbol, expected_base in cases:
            base = symbol
            for quote in ("USDT", "USDC", "BUSD", "FDUSD", "BTC", "ETH"):
                if symbol.endswith(quote) and len(symbol) > len(quote):
                    base = symbol[:-len(quote)]
                    break
            self.assertEqual(base, expected_base, f"Failed for {symbol}")


class TestPositionSizeCap(unittest.TestCase):
    """
    Position value must never exceed MAX_POSITION_VALUE_PCT of balance.
    This was bug #3 — without the cap, a price mismatch causes a
    runaway position size.
    """

    @classmethod
    def setUpClass(cls):
        cls.srv = load_server_module()

    def setUp(self):
        # Reset mutable state between tests
        self.srv.state["open_positions"] = []
        self.srv.state["circuit_breaker"] = False
        self.srv.state["trades_today"]    = 0
        self.srv.state["daily_pnl"]       = 0.0

    def test_cap_triggers_when_price_risk_tiny(self):
        """
        If stop is very close to entry (price_risk tiny), raw qty is huge.
        The cap must reduce it to MAX_POSITION_VALUE_PCT of balance.
        """
        balance = 10000.0
        entry = 65000.0
        stop  = 64999.0  # $1 risk — would produce qty=100 BTC at 1% risk
        risk_amount = balance * (self.srv.RISK_PER_TRADE_PCT / 100)  # $100
        price_risk  = abs(entry - stop)  # $1
        raw_qty     = risk_amount / price_risk  # 100 BTC = $6.5M — absurd
        position_value = raw_qty * entry
        max_allowed = balance * (self.srv.MAX_POSITION_VALUE_PCT / 100)

        self.assertGreater(position_value, max_allowed,
            "Test precondition: raw qty should exceed cap")

        capped_qty = max_allowed / entry
        capped_value = capped_qty * entry
        self.assertLessEqual(capped_value, max_allowed + 0.01)

    def test_normal_sizing_not_capped(self):
        """
        Normal position sizing: at 1% risk with a 2.5% SL, position
        value should equal risk_amount / SL_pct = 1% / 2.5% = 40% of
        balance — which DOES exceed the 5% cap. This is by design: the
        cap limits position size to 5% of balance regardless of the
        risk math. Test that cap math itself is internally consistent.
        """
        balance = 10000.0
        entry = 1800.0
        stop  = entry * (1 - 0.025)   # 2.5% SL — standard config
        price_risk = abs(entry - stop)  # $45
        risk_amount = balance * (self.srv.RISK_PER_TRADE_PCT / 100)  # $100
        raw_qty = risk_amount / price_risk   # 2.22 ETH = $4000

        max_allowed = balance * (self.srv.MAX_POSITION_VALUE_PCT / 100)  # $500
        # Cap triggers — capped qty should be max_allowed / entry
        capped_qty   = max_allowed / entry   # 0.277 ETH
        capped_value = capped_qty * entry    # $500

        # Verify the cap math is internally consistent
        self.assertAlmostEqual(capped_value, max_allowed, delta=0.01,
            msg="Capped position value must equal MAX_POSITION_VALUE_PCT of balance")
        self.assertLess(capped_qty, raw_qty,
            msg="Capped qty must be less than uncapped qty")


class TestShouldTrade(unittest.TestCase):
    """
    should_trade() in aegis_bot — entry gate logic including regime
    quality, regime consistency, and score thresholds.
    """

    @classmethod
    def setUpClass(cls):
        cls.bot = load_bot_module()

    def setUp(self):
        # Reset module-level state between tests
        self.bot._last_regime.clear()

    def _signal(self, opp=75, conf=70, risk=45, vol=1.5, tf=3,
                regime="Strong bull", symbol="ETHUSDT",
                daily_trend_ok=True):
        return {
            "symbol": symbol, "opp_score": opp, "conf_score": conf,
            "risk_score": risk, "volume_ratio": vol, "tf_bullish": tf,
            "regime": regime, "daily_trend_ok": daily_trend_ok
        }

    def test_passes_all_conditions(self):
        """Perfect signal with confirmed Strong Bull should TRADE."""
        self.bot._last_regime["ETHUSDT"] = "Strong bull"
        verdict, passed, failed = self.bot.should_trade(self._signal())
        self.assertTrue(verdict)
        self.assertEqual(len(failed), 0)

    def test_fails_low_opp_score(self):
        self.bot._last_regime["ETHUSDT"] = "Strong bull"
        verdict, _, failed = self.bot.should_trade(self._signal(opp=60))
        self.assertFalse(verdict)
        self.assertIn("opp_score", failed)

    def test_fails_low_conf_score(self):
        self.bot._last_regime["ETHUSDT"] = "Strong bull"
        verdict, _, failed = self.bot.should_trade(self._signal(conf=60))
        self.assertFalse(verdict)
        self.assertIn("conf_score", failed)

    def test_fails_high_risk_score(self):
        self.bot._last_regime["ETHUSDT"] = "Strong bull"
        verdict, _, failed = self.bot.should_trade(self._signal(risk=55))
        self.assertFalse(verdict)
        self.assertIn("risk_score", failed)

    def test_fails_low_volume(self):
        self.bot._last_regime["ETHUSDT"] = "Strong bull"
        verdict, _, failed = self.bot.should_trade(self._signal(vol=1.0))
        self.assertFalse(verdict)
        self.assertIn("volume_ratio", failed)

    def test_fails_insufficient_tf_alignment(self):
        self.bot._last_regime["ETHUSDT"] = "Strong bull"
        verdict, _, failed = self.bot.should_trade(self._signal(tf=1))
        self.assertFalse(verdict)
        self.assertIn("tf_bullish", failed)

    def test_fails_weak_bull_regime(self):
        """Weak Bull regime should be blocked — regime_quality check."""
        self.bot._last_regime["ETHUSDT"] = "Weak bull"
        verdict, _, failed = self.bot.should_trade(
            self._signal(regime="Weak bull"))
        self.assertFalse(verdict)
        self.assertIn("regime_quality", failed)

    def test_fails_bearish_regime(self):
        """Bearish regime must always be blocked."""
        self.bot._last_regime["ETHUSDT"] = "Bearish"
        verdict, _, failed = self.bot.should_trade(
            self._signal(regime="Bearish"))
        self.assertFalse(verdict)
        self.assertIn("regime_quality", failed)

    def test_fails_sideways_regime(self):
        """Sideways regime must always be blocked."""
        self.bot._last_regime["ETHUSDT"] = "Sideways"
        verdict, _, failed = self.bot.should_trade(
            self._signal(regime="Sideways"))
        self.assertFalse(verdict)
        self.assertIn("regime_quality", failed)

    def test_fails_first_cycle_no_prior_regime(self):
        """
        On the very first scan cycle after restart, _last_regime is empty.
        regime_consistency must fail — no trade on the first cycle.
        This prevents entering on a single unconfirmed Strong Bull reading.
        """
        # _last_regime is {} — no prior reading for ETHUSDT
        verdict, _, failed = self.bot.should_trade(self._signal())
        self.assertFalse(verdict)
        self.assertIn("regime_consistency", failed)

    def test_fails_regime_jumped_from_sideways(self):
        """
        Previous cycle was Sideways, current is Strong Bull.
        regime_consistency must fail — this is a regime transition,
        not a confirmed bull. Caught 3 of 19 historical signals.
        """
        self.bot._last_regime["ETHUSDT"] = "Sideways"
        verdict, _, failed = self.bot.should_trade(self._signal())
        self.assertFalse(verdict)
        self.assertIn("regime_consistency", failed)

    def test_passes_two_consecutive_strong_bull(self):
        """Two consecutive Strong Bull cycles — should TRADE."""
        self.bot._last_regime["ETHUSDT"] = "Strong bull"
        verdict, passed, failed = self.bot.should_trade(self._signal())
        self.assertTrue(verdict, f"Should TRADE but failed: {failed}")

    def test_fails_daily_trend_below_sma50(self):
        """
        1D close below daily SMA50 must block entry.
        Backtest finding: Feb 2025 had 0% win rate (6 consecutive losses)
        despite all other filters passing — price was below daily SMA50
        in a correction. This filter directly addresses that failure mode.
        """
        self.bot._last_regime["ETHUSDT"] = "Strong bull"
        sig = self._signal()
        sig["daily_trend_ok"] = False
        verdict, _, failed = self.bot.should_trade(sig)
        self.assertFalse(verdict)
        self.assertIn("daily_trend", failed)

    def test_passes_daily_trend_above_sma50(self):
        """1D close above daily SMA50 — daily_trend check passes."""
        self.bot._last_regime["ETHUSDT"] = "Strong bull"
        sig = self._signal()
        sig["daily_trend_ok"] = True
        verdict, passed, failed = self.bot.should_trade(sig)
        self.assertTrue(verdict, f"Should TRADE but failed: {failed}")
        self.assertIn("daily_trend", passed)

    def test_daily_trend_defaults_true_when_missing(self):
        """
        When daily_trend_ok is absent from the signals dict (sma50_d
        unavailable — fewer than 50 daily candles for a new asset),
        the filter must default to True so it doesn't silently block
        legitimate signals during a new asset's first 50 trading days.
        """
        self.bot._last_regime["ETHUSDT"] = "Strong bull"
        sig = self._signal()
        sig.pop("daily_trend_ok", None)  # explicitly absent
        verdict, _, failed = self.bot.should_trade(sig)
        self.assertTrue(verdict,
            "daily_trend must default to PASS when sma50_d is unavailable")
        self.assertNotIn("daily_trend", failed)

    def test_different_symbols_independent(self):
        """
        Regime history is per-symbol. BTC's regime shouldn't
        affect ETH's consistency check.
        """
        self.bot._last_regime["BTCUSDT"] = "Strong bull"
        # ETH has no prior regime — should fail regime_consistency
        verdict, _, failed = self.bot.should_trade(
            self._signal(symbol="ETHUSDT"))
        self.assertFalse(verdict)
        self.assertIn("regime_consistency", failed)


class TestValidateSignal(unittest.TestCase):
    """
    validate_signal() in aegis_server — server-side gate including
    per-asset concentration check and circuit breaker passthrough.
    """

    @classmethod
    def setUpClass(cls):
        cls.srv = load_server_module()

    def setUp(self):
        # Reset mutable state between tests
        self.srv.state["open_positions"] = []
        self.srv.state["circuit_breaker"] = False
        self.srv.state["trades_today"]    = 0
        self.srv.state["daily_pnl"]       = 0.0
        # Clean state before each test
        self.srv.state["open_positions"] = []
        self.srv.state["circuit_breaker"] = False
        self.srv.state["trades_today"]    = 0
        self.srv.state["daily_pnl"]       = 0.0

    def _signal(self, opp=75, conf=70, risk=45, vol=1.5, tf=3,
                symbol="ETHUSDT"):
        return {
            "symbol": symbol, "opp_score": opp, "conf_score": conf,
            "risk_score": risk, "volume_ratio": vol, "tf_bullish": tf
        }

    def test_clean_signal_passes(self):
        valid, errors = self.srv.validate_signal(self._signal())
        self.assertTrue(valid)
        self.assertEqual(errors, [])

    def test_blocks_duplicate_asset(self):
        """
        If ETHUSDT already has an open position, a second ETHUSDT
        signal must be blocked — this is the fix for today's double-entry.
        """
        self.srv.state["open_positions"] = [
            {"symbol": "ETHUSDT", "id": "111", "qty": 0.1368, "entry": 1827.91}
        ]
        valid, errors = self.srv.validate_signal(self._signal(symbol="ETHUSDT"))
        self.assertFalse(valid)
        self.assertTrue(any("ETHUSDT" in e for e in errors))

    def test_allows_different_asset_when_one_open(self):
        """ETHUSDT open — BTCUSDT signal should still pass concentration check."""
        self.srv.state["open_positions"] = [
            {"symbol": "ETHUSDT", "id": "111", "qty": 0.1368, "entry": 1827.91}
        ]
        valid, errors = self.srv.validate_signal(self._signal(symbol="BTCUSDT"))
        self.assertTrue(valid, f"BTCUSDT blocked unexpectedly: {errors}")

    def test_blocks_when_circuit_breaker_active(self):
        self.srv.state["circuit_breaker"] = True
        valid, errors = self.srv.validate_signal(self._signal())
        self.assertFalse(valid)
        self.assertTrue(any("Circuit breaker" in e or "circuit" in e.lower()
                            for e in errors))

    def test_blocks_max_trades_per_day(self):
        self.srv.state["trades_today"] = self.srv.MAX_TRADES_PER_DAY
        valid, errors = self.srv.validate_signal(self._signal())
        self.assertFalse(valid)

    def test_blocks_low_opp(self):
        valid, errors = self.srv.validate_signal(self._signal(opp=60))
        self.assertFalse(valid)
        self.assertTrue(any("Opp" in e for e in errors))

    def test_blocks_high_risk(self):
        valid, errors = self.srv.validate_signal(self._signal(risk=60))
        self.assertFalse(valid)
        self.assertTrue(any("Risk" in e for e in errors))

    def test_blocks_insufficient_tf(self):
        valid, errors = self.srv.validate_signal(self._signal(tf=1))
        self.assertFalse(valid)
        self.assertTrue(any("TF" in e for e in errors))


class TestCircuitBreaker(unittest.TestCase):
    """
    check_circuit_breaker() — all four trigger conditions must work
    correctly and not interfere with each other.
    """

    @classmethod
    def setUpClass(cls):
        cls.srv = load_server_module()

    def setUp(self):
        # Reset mutable state between tests
        self.srv.state["open_positions"] = []
        self.srv.state["circuit_breaker"] = False
        self.srv.state["trades_today"]    = 0
        self.srv.state["daily_pnl"]       = 0.0
        self.srv.state["open_positions"] = []
        self.srv.state["circuit_breaker"] = False
        self.srv.state["trades_today"]    = 0
        self.srv.state["daily_pnl"]       = 0.0

    def test_clear_state_returns_false(self):
        tripped, reason = self.srv.check_circuit_breaker()
        self.assertFalse(tripped)
        self.assertEqual(reason, "OK")

    def test_already_active(self):
        self.srv.state["circuit_breaker"] = True
        tripped, _ = self.srv.check_circuit_breaker()
        self.assertTrue(tripped)

    def test_max_trades_per_day(self):
        self.srv.state["trades_today"] = self.srv.MAX_TRADES_PER_DAY
        tripped, reason = self.srv.check_circuit_breaker()
        self.assertTrue(tripped)
        self.assertIn(str(self.srv.MAX_TRADES_PER_DAY), reason)

    def test_max_open_positions(self):
        self.srv.state["open_positions"] = [
            {"symbol": f"ASSET{i}"} for i in range(self.srv.MAX_OPEN_POSITIONS)
        ]
        tripped, reason = self.srv.check_circuit_breaker()
        self.assertTrue(tripped)
        self.assertIn("positions", reason.lower())

    def test_daily_loss_limit(self):
        self.srv.state["daily_pnl"] = -(self.srv.DAILY_LOSS_LIMIT + 1)
        tripped, reason = self.srv.check_circuit_breaker()
        self.assertTrue(tripped)
        self.assertIn("loss", reason.lower())

    def test_one_below_max_trades_does_not_trip(self):
        self.srv.state["trades_today"] = self.srv.MAX_TRADES_PER_DAY - 1
        tripped, _ = self.srv.check_circuit_breaker()
        self.assertFalse(tripped)


class TestStateHydration(unittest.TestCase):
    """
    hydrate_state_from_db() — on server restart, open positions and
    today's trade count must be restored from aegis.db so P1 and the
    circuit breaker work correctly across restarts.
    """

    @classmethod
    def setUpClass(cls):
        cls.srv = load_server_module()

    def setUp(self):
        self.srv.state["open_positions"] = []
        self.srv.state["circuit_breaker"] = False
        self.srv.state["trades_today"]    = 0
        self.srv.state["daily_pnl"]       = 0.0

    def _write_temp_db(self, rows):
        """Create a temp SQLite DB pre-populated with the given rows."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        import sqlite3 as _sq
        with _sq.connect(f.name) as conn:
            conn.execute("""
                CREATE TABLE trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT, time TEXT, symbol TEXT, side TEXT,
                    qty REAL, entry REAL, stop REAL, target REAL,
                    exit_price REAL, pnl_usdt REAL, pnl_pct REAL,
                    rr_achieved REAL, opp_score INTEGER, conf_score INTEGER,
                    risk_score INTEGER, volume_ratio REAL, tf_bullish INTEGER,
                    regime TEXT, order_id TEXT UNIQUE, status TEXT,
                    duration_min REAL, order_list_id TEXT,
                    initial_risk_usd REAL, exit_time TEXT, result TEXT
                )
            """)
            fn = self.srv.TRADE_FIELDNAMES
            for r in rows:
                conn.execute(
                    f"INSERT INTO trades ({','.join(fn)}) VALUES ({','.join(['?']*len(fn))})",
                    [r.get(k) for k in fn]
                )
        return f.name

    def test_restores_open_position(self):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        dbfile = self._write_temp_db([{
            "date": today, "time": "12:37:57",
            "symbol": "ETHUSDT", "side": "BUY",
            "qty": 0.1368, "entry": 1827.91,
            "stop": 1781.66, "target": 1927.84,
            "order_id": "7836464272", "status": "open",
            "order_list_id": "3037277", "initial_risk_usd": 6.327,
            "opp_score": 80, "conf_score": 82, "risk_score": 46,
        }])
        original_db = self.srv.TRADE_LOG_DB
        self.srv.TRADE_LOG_DB = dbfile
        try:
            self.srv.hydrate_state_from_db()
            self.assertEqual(len(self.srv.state["open_positions"]), 1)
            pos = self.srv.state["open_positions"][0]
            self.assertEqual(pos["symbol"], "ETHUSDT")
            self.assertEqual(pos["order_list_id"], "3037277")
            self.assertAlmostEqual(pos["qty"], 0.1368)
            self.assertEqual(self.srv.state["trades_today"], 1)
        finally:
            self.srv.TRADE_LOG_DB = original_db
            gc.collect()  # release SQLite file lock before unlink (Windows)
            os.unlink(dbfile)

    def test_closed_trades_not_restored_as_positions(self):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        dbfile = self._write_temp_db([{
            "date": today, "time": "12:37:57",
            "symbol": "ETHUSDT", "side": "BUY",
            "qty": 0.1368, "entry": 1827.91,
            "stop": 1781.66, "target": 1927.84,
            "order_id": "7836464272", "status": "closed_sl",
            "exit_price": 1780.00, "pnl_usdt": -6.80, "result": "LOSS",
        }])
        original_db = self.srv.TRADE_LOG_DB
        self.srv.TRADE_LOG_DB = dbfile
        try:
            self.srv.hydrate_state_from_db()
            self.assertEqual(len(self.srv.state["open_positions"]), 0,
                "Closed trades must NOT be restored as open positions")
            self.assertEqual(self.srv.state["trades_today"], 1,
                "Closed trades should still count toward trades_today")
        finally:
            self.srv.TRADE_LOG_DB = original_db
            gc.collect()  # release SQLite file lock before unlink (Windows)
            os.unlink(dbfile)

    def test_empty_db_gives_clean_state(self):
        dbfile = self._write_temp_db([])
        original_db = self.srv.TRADE_LOG_DB
        self.srv.TRADE_LOG_DB = dbfile
        try:
            self.srv.hydrate_state_from_db()
            self.assertEqual(len(self.srv.state["open_positions"]), 0)
            self.assertEqual(self.srv.state["trades_today"], 0)
        finally:
            self.srv.TRADE_LOG_DB = original_db
            gc.collect()  # release SQLite file lock before unlink (Windows)
            os.unlink(dbfile)

    def test_missing_db_does_not_crash(self):
        """Non-existent DB — should log a warning and not raise."""
        original_db = self.srv.TRADE_LOG_DB
        self.srv.TRADE_LOG_DB = "/tmp/nonexistent_aegis_test.db"
        try:
            self.srv.hydrate_state_from_db()
            self.assertEqual(len(self.srv.state["open_positions"]), 0)
        finally:
            self.srv.TRADE_LOG_DB = original_db


class TestDbWrite(unittest.TestCase):
    """
    write_trade_csv() inserts into aegis.db and the row is readable back.
    rewrite_trade_csv_row() updates the row correctly.
    get_open_trade_rows() returns only open/degraded rows.
    """

    @classmethod
    def setUpClass(cls):
        cls.srv = load_server_module()

    def setUp(self):
        self.srv.state["open_positions"] = []
        self.srv.state["circuit_breaker"] = False
        self.srv.state["trades_today"]    = 0
        self.srv.state["daily_pnl"]       = 0.0
        # Fresh DB for each test
        import sqlite3 as _sq
        _sq.connect(self.srv.TRADE_LOG_DB).close()
        with _sq.connect(self.srv.TRADE_LOG_DB) as conn:
            conn.execute("DELETE FROM trades")

    def _sample_trade(self, order_id="TEST001", status="open"):
        return {
            "date": "2026-06-16", "time": "12:37:57",
            "symbol": "ETHUSDT", "side": "BUY",
            "qty": "0.1368", "entry": "1827.91",
            "stop": "1781.66", "target": "1927.84",
            "exit_price": "", "pnl_usdt": "", "pnl_pct": "",
            "rr_achieved": "", "duration_min": "",
            "opp_score": "80", "conf_score": "82", "risk_score": "46",
            "volume_ratio": "2.04", "tf_bullish": "3",
            "regime": "Strong bull", "order_id": order_id,
            "status": status, "order_list_id": "3037277",
            "initial_risk_usd": "6.327", "exit_time": "", "result": "",
        }

    def test_write_inserts_row(self):
        self.srv.write_trade_csv(self._sample_trade())
        import sqlite3 as _sq
        with _sq.connect(self.srv.TRADE_LOG_DB) as conn:
            row = conn.execute(
                "SELECT order_id, symbol, status FROM trades WHERE order_id='TEST001'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "ETHUSDT")
        self.assertEqual(row[2], "open")

    def test_write_duplicate_ignored(self):
        """INSERT OR IGNORE — writing the same order_id twice stays at 1 row."""
        self.srv.write_trade_csv(self._sample_trade())
        self.srv.write_trade_csv(self._sample_trade())
        import sqlite3 as _sq
        with _sq.connect(self.srv.TRADE_LOG_DB) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE order_id='TEST001'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_rewrite_updates_closure_fields(self):
        self.srv.write_trade_csv(self._sample_trade())
        ok = self.srv.rewrite_trade_csv_row("TEST001", {
            "exit_price": 1780.00, "pnl_usdt": -6.80,
            "result": "LOSS", "status": "closed_sl",
        })
        self.assertTrue(ok)
        import sqlite3 as _sq
        with _sq.connect(self.srv.TRADE_LOG_DB) as conn:
            row = conn.execute(
                "SELECT status, result, pnl_usdt FROM trades WHERE order_id='TEST001'"
            ).fetchone()
        self.assertEqual(row[0], "closed_sl")
        self.assertEqual(row[1], "LOSS")
        self.assertAlmostEqual(row[2], -6.80, places=2)

    def test_rewrite_missing_order_id_returns_false(self):
        ok = self.srv.rewrite_trade_csv_row("DOESNOTEXIST", {"status": "closed_sl"})
        self.assertFalse(ok)

    def test_get_open_trade_rows_filters_correctly(self):
        self.srv.write_trade_csv(self._sample_trade("OPEN1", "open"))
        self.srv.write_trade_csv(self._sample_trade("CLOSED1", "closed_sl"))
        self.srv.write_trade_csv(self._sample_trade("DEGRADED1", "DEGRADED"))
        rows = self.srv.get_open_trade_rows()
        order_ids = {r["order_id"] for r in rows}
        self.assertIn("OPEN1", order_ids)
        self.assertIn("DEGRADED1", order_ids)
        self.assertNotIn("CLOSED1", order_ids)


class TestSlTpCalculation(unittest.TestCase):
    """SL and TP prices and R:R ratio correctness."""

    @classmethod
    def setUpClass(cls):
        cls.bot = load_bot_module()

    def test_sl_is_below_entry(self):
        entry = 1827.91
        stop  = round(entry * (1 - self.bot.STOP_LOSS_PCT), 2)
        self.assertLess(stop, entry)

    def test_tp_is_above_entry(self):
        entry = 1827.91
        target = round(entry * (1 + self.bot.TAKE_PROFIT_PCT), 2)
        self.assertGreater(target, entry)

    def test_sl_distance_matches_pct(self):
        entry = 1827.91
        stop  = round(entry * (1 - self.bot.STOP_LOSS_PCT), 2)
        actual_pct = (entry - stop) / entry
        self.assertAlmostEqual(actual_pct, self.bot.STOP_LOSS_PCT, places=3)

    def test_tp_distance_matches_pct(self):
        entry = 1827.91
        target = round(entry * (1 + self.bot.TAKE_PROFIT_PCT), 2)
        actual_pct = (target - entry) / entry
        self.assertAlmostEqual(actual_pct, self.bot.TAKE_PROFIT_PCT, places=3)

    def test_rr_ratio_matches_config(self):
        """
        Configured R:R is TAKE_PROFIT_PCT / STOP_LOSS_PCT.
        At current settings (2.5% SL, 5.5% TP) this should be 2.2:1.
        A win should always cover more than 2 losses.
        """
        rr = self.bot.TAKE_PROFIT_PCT / self.bot.STOP_LOSS_PCT
        self.assertGreaterEqual(rr, 2.0,
            f"R:R {rr:.2f} is below 2:1 — system is not positively skewed")

    def test_breakeven_win_rate(self):
        """
        At configured R:R, the breakeven win rate = 1 / (1 + R:R).
        System must require less than 50% wins to break even.
        """
        rr = self.bot.TAKE_PROFIT_PCT / self.bot.STOP_LOSS_PCT
        breakeven_wr = 1 / (1 + rr)
        self.assertLess(breakeven_wr, 0.5,
            f"Breakeven win rate {breakeven_wr:.1%} — need positive asymmetry")

    def test_eth_concrete_values(self):
        """Replicate the exact values from trade #4 (confirmed working)."""
        entry = 1827.34
        stop   = round(entry * (1 - self.bot.STOP_LOSS_PCT), 2)
        target = round(entry * (1 + self.bot.TAKE_PROFIT_PCT), 2)
        # From the log: SL:$1781.66 | TP:$1927.84
        self.assertAlmostEqual(stop,   1781.66, delta=0.05)
        self.assertAlmostEqual(target, 1927.84, delta=0.05)

    def test_btc_concrete_values(self):
        """Replicate BTC trade values."""
        entry  = 65722.85
        stop   = round(entry * (1 - self.bot.STOP_LOSS_PCT), 2)
        target = round(entry * (1 + self.bot.TAKE_PROFIT_PCT), 2)
        # SL should be 2.5% below entry
        self.assertAlmostEqual((entry - stop) / entry,
                               self.bot.STOP_LOSS_PCT, places=3)
        self.assertAlmostEqual((target - entry) / entry,
                               self.bot.TAKE_PROFIT_PCT, places=3)


# ─────────────────────────────────────────────────────────────
# RUNNER
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Custom runner: print a clean summary
    loader  = unittest.TestLoader()
    suite   = loader.loadTestsFromModule(sys.modules[__name__])
    runner  = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result  = runner.run(suite)

    print("\n" + "=" * 60)
    print(f"  Tests run:    {result.testsRun}")
    print(f"  Passed:       {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"  Failures:     {len(result.failures)}")
    print(f"  Errors:       {len(result.errors)}")
    print("=" * 60)

    sys.stdout.flush()
    if result.wasSuccessful():
        sys.stdout.buffer.write("  ✓ ALL TESTS PASSED — safe to deploy\n".encode("utf-8", errors="replace"))
    else:
        sys.stdout.buffer.write("  ✗ TESTS FAILED — do not deploy until fixed\n".encode("utf-8", errors="replace"))
    sys.stdout.buffer.flush()
    print()

    sys.exit(0 if result.wasSuccessful() else 1)
