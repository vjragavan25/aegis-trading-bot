"""
Aegis AI Reasoning Module
==========================
Connects the Aegis trading bot to Claude API.
Adds intelligence layer: plain-English analysis, signal reasoning,
morning briefs, anomaly detection, and Telegram alerts.

How it works:
  - Called by aegis_bot.py after every scan cycle
  - Sends market data to Claude API
  - Claude reasons about conditions and writes analysis
  - Results logged to journal and optionally sent to Telegram

Setup:
  1. Get your Anthropic API key from console.anthropic.com
  2. Paste it on line 32 below
  3. (Optional) Set up Telegram bot for phone alerts — see lines 35-38
  4. Import this module in aegis_bot.py — instructions at bottom of file
"""

import json
import urllib.request
import urllib.error
import os
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────
try:
    from aegis_secrets import ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
except ImportError:
    ANTHROPIC_API_KEY  = "PASTE_YOUR_ANTHROPIC_API_KEY_HERE"
    TELEGRAM_BOT_TOKEN = ""
    TELEGRAM_CHAT_ID   = ""

CLAUDE_MODEL = "claude-sonnet-4-6"
MAX_TOKENS   = 1000

# Output files (same folder as server)
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
AI_BRIEF_FILE = os.path.join(SCRIPT_DIR, "aegis_ai_briefs.txt")
AI_LOG_FILE   = os.path.join(SCRIPT_DIR, "aegis_ai_log.json")

# How often to generate a full brief (every N cycles)
BRIEF_EVERY_N_CYCLES = 4   # Every 4 cycles = every hour

# ─────────────────────────────────────────────────────
# CLAUDE API CALL
# ─────────────────────────────────────────────────────
def ask_claude(prompt: str, system: str = None) -> str:
    """Send a prompt to Claude and return the response text."""
    if ANTHROPIC_API_KEY == "PASTE_YOUR_ANTHROPIC_API_KEY_HERE":
        return "[AI reasoning disabled — add your Anthropic API key to aegis_ai.py line 32]"

    messages = [{"role": "user", "content": prompt}]
    body = {
        "model":      CLAUDE_MODEL,
        "max_tokens": MAX_TOKENS,
        "messages":   messages
    }
    if system:
        body["system"] = system

    headers = {
        "Content-Type":      "application/json",
        "x-api-key":         ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01"
    }

    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read())
            return result["content"][0]["text"].strip()
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        return f"[Claude API error {e.code}: {err[:200]}]"
    except Exception as e:
        return f"[Claude API unavailable: {str(e)[:100]}]"

# ─────────────────────────────────────────────────────
# SYSTEM PROMPT — Claude's role in this system
# ─────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Aegis, an embedded AI analyst inside an automated crypto trading system.

Your role is to reason about market conditions from live data and provide clear, 
actionable intelligence in plain English. You are not a chatbot — you are an analytical 
engine. Be direct, evidence-based, and concise.

Rules:
- Never use hype or emotional language
- Separate facts from probabilities from speculation explicitly  
- Always quantify confidence where possible
- Point out what could invalidate the analysis
- Keep responses under 200 words unless a full brief is requested
- Format for terminal output — no markdown headers, plain text only"""

# ─────────────────────────────────────────────────────
# SIGNAL REASONER
# Analyses a single asset scan result
# ─────────────────────────────────────────────────────
def reason_about_signal(signal: dict) -> str:
    """
    Takes a scan result dict and returns Claude's reasoning.
    Called when any asset scores above 60 — worth thinking about.
    """
    sym    = signal.get("symbol", "?")
    opp    = signal.get("opp_score", 0)
    conf   = signal.get("conf_score", 0)
    risk   = signal.get("risk_score", 0)
    vol    = signal.get("volume_ratio", 0)
    tf     = signal.get("tf_bullish", 0)
    pct    = signal.get("pct_24h", 0)
    regime = signal.get("regime", "unknown")
    action = signal.get("action", "WAIT")
    failed = signal.get("failed", [])

    prompt = f"""Asset: {sym}
Opportunity score: {opp}/100
Confidence score: {conf}/100  
Risk score: {risk}/100
Volume ratio: {vol:.2f}x average
Timeframes bullish: {tf}/3
24h momentum: {pct:+.2f}%
Market regime: {regime}
Bot decision: {action}
Failed conditions: {", ".join(failed) if failed else "none"}

In 3-4 sentences: what does this data tell you about the quality of this setup? 
What specifically needs to change before this becomes actionable?
What is the single most important thing to watch?"""

    return ask_claude(prompt, SYSTEM_PROMPT)

# ─────────────────────────────────────────────────────
# CYCLE BRIEF
# Full analysis after each scan cycle
# ─────────────────────────────────────────────────────
def generate_cycle_brief(scan_results: list, cycle_num: int) -> str:
    """
    Generates a full market brief from a complete scan cycle.
    Returns plain-English analysis of all three assets.
    """
    ts = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M UTC")

    # Build the data summary
    asset_lines = []
    for r in scan_results:
        asset_lines.append(
            f"{r['symbol']}: Opp {r['opp_score']}/100 | Conf {r['conf_score']}/100 | "
            f"Risk {r['risk_score']}/100 | Vol {r['volume_ratio']:.2f}x | "
            f"TF {r['tf_bullish']}/3 | {r['pct_24h']:+.2f}% | "
            f"Regime: {r['regime']} | Decision: {r['action']}"
        )

    # Identify best setup
    best = max(scan_results, key=lambda x: x["opp_score"]) if scan_results else None
    worst = min(scan_results, key=lambda x: x["opp_score"]) if scan_results else None

    prompt = f"""Scan cycle #{cycle_num} — {ts}

LIVE MARKET DATA:
{chr(10).join(asset_lines)}

Write a professional market brief covering:
1. Overall market condition (1 sentence — what regime are we in?)
2. Best setup of the three and specifically why (2 sentences)
3. The single biggest risk or warning sign right now (1 sentence)  
4. What trigger would change the picture from WAIT to TRADE (1 sentence)
5. Asset to avoid entirely right now and why (1 sentence)

Keep it under 150 words. Be direct. No fluff."""

    brief = ask_claude(prompt, SYSTEM_PROMPT)

    # Add header
    header = f"\n{'='*56}\nAegis AI Brief — Cycle #{cycle_num} — {ts}\n{'='*56}\n"
    full_brief = header + brief + "\n"

    # Write to file
    try:
        with open(AI_BRIEF_FILE, "a", encoding="utf-8") as f:
            f.write(full_brief)
    except Exception as e:
        print(f"[AI] Brief write failed: {e}")

    return brief

# ─────────────────────────────────────────────────────
# MORNING BRIEF
# Called once per day on first scan after 06:00 UTC
# ─────────────────────────────────────────────────────
def generate_morning_brief(scan_results: list, trade_log: list = None) -> str:
    """
    Generates a comprehensive morning brief.
    Covers overnight action, current conditions, and what to watch today.
    """
    ts = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M UTC")

    asset_lines = []
    for r in scan_results:
        asset_lines.append(
            f"{r['symbol']}: Opp {r['opp_score']}/100 | Conf {r['conf_score']}/100 | "
            f"Risk {r['risk_score']}/100 | Vol {r['volume_ratio']:.2f}x | "
            f"TF {r['tf_bullish']}/3 | {r['pct_24h']:+.2f}% | Regime: {r['regime']}"
        )

    # Include recent trades if any
    trade_section = ""
    if trade_log:
        recent = trade_log[-3:]
        trade_lines = [
            f"  - {t.get('symbol','?')} {t.get('side','?')} @ ${t.get('entry','?')} "
            f"| Status: {t.get('status','?')} | PnL: {t.get('pnl_usdt','open')}"
            for t in recent
        ]
        trade_section = f"\nRECENT TRADES:\n" + "\n".join(trade_lines)

    prompt = f"""AEGIS MORNING BRIEF REQUEST — {ts}

CURRENT MARKET SNAPSHOT:
{chr(10).join(asset_lines)}
{trade_section}

Write a morning brief that covers:
1. Market regime summary — what kind of day is setting up?
2. The most important thing that happened or is happening
3. Best opportunity to watch today with specific trigger conditions
4. Key risk to be aware of today
5. One-line action summary: what should the bot do today?

Maximum 200 words. Professional tone. Evidence-based."""

    brief = ask_claude(prompt, SYSTEM_PROMPT)

    header = f"\n{'*'*56}\nAEGIS MORNING BRIEF — {ts}\n{'*'*56}\n"
    full_brief = header + brief + "\n" + "*"*56 + "\n"

    try:
        with open(AI_BRIEF_FILE, "a", encoding="utf-8") as f:
            f.write(full_brief)
    except Exception as e:
        print(f"[AI] Morning brief write failed: {e}")

    return brief

# ─────────────────────────────────────────────────────
# ANOMALY DETECTOR
# Spots unusual patterns in recent signal history
# ─────────────────────────────────────────────────────
def detect_anomalies(recent_signals: list) -> str:
    """
    Analyses the last N signals for anomalies.
    Called every 8 cycles (every 2 hours).
    Returns plain-English anomaly report or 'No anomalies detected.'
    """
    if not recent_signals or len(recent_signals) < 3:
        return "Insufficient signal history for anomaly detection."

    # Summarise recent signals concisely
    lines = []
    for s in recent_signals[-12:]:  # last 12 scans = 3 hours
        lines.append(
            f"{s.get('datetime','?')} {s.get('symbol','?')}: "
            f"Opp:{s.get('opp_score','?')} Vol:{s.get('volume_ratio','?')}x "
            f"TF:{s.get('tf_bullish','?')}/3 {s.get('verdict','?')}"
        )

    prompt = f"""Recent signal history (last 3 hours):
{chr(10).join(lines)}

Identify any anomalies, unusual patterns, or warning signs in this data.
Look for: sudden score drops, volume spikes, regime changes, or inconsistencies.
If nothing unusual: respond with exactly 'No anomalies detected.'
If something notable: describe it in 2 sentences maximum."""

    return ask_claude(prompt, SYSTEM_PROMPT)

# ─────────────────────────────────────────────────────
# TRADE COMMENTARY
# Called when bot places a trade
# ─────────────────────────────────────────────────────
def comment_on_trade(trade: dict) -> str:
    """
    Generates commentary when a trade is placed.
    Explains why it fired, what to watch, and what would invalidate it.
    """
    prompt = f"""The automated bot just placed this trade:

Symbol: {trade.get('symbol','?')}
Side: {trade.get('side','?')}
Entry: ${trade.get('entry','?')}
Stop loss: ${trade.get('stop','?')} ({trade.get('risk_score','?')} risk score)
Take profit: ${trade.get('target','?')}
Opportunity score: {trade.get('opp_score','?')}/100
Confidence: {trade.get('conf_score','?')}/100
Volume ratio: {trade.get('volume_ratio','?')}x
Timeframes bullish: {trade.get('tf_bullish','?')}/3
Market regime: {trade.get('regime','?')}

In exactly 3 sentences:
1. Why this trade fired (what conditions aligned)
2. What to monitor while it's open
3. What would invalidate the thesis (besides hitting stop-loss)"""

    commentary = ask_claude(prompt, SYSTEM_PROMPT)

    # Log it
    log_entry = {
        "type":       "trade_commentary",
        "datetime":   datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "trade":      trade,
        "commentary": commentary
    }
    _append_log(log_entry)

    return commentary

# ─────────────────────────────────────────────────────
# TELEGRAM ALERTS
# ─────────────────────────────────────────────────────
def send_telegram(message: str, parse_mode: str = "HTML") -> bool:
    """
    Sends a message to your Telegram chat.
    Returns True if successful, False otherwise.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False  # Telegram not configured

    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    body = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": parse_mode
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            result = json.loads(r.read())
            return result.get("ok", False)
    except Exception as e:
        print(f"[Telegram] Send failed: {e}")
        return False

def alert_trade_fired(trade: dict, commentary: str):
    """Sends a Telegram alert when a trade is placed."""
    sym  = trade.get("symbol","?")
    side = trade.get("side","?")
    entry = trade.get("entry","?")
    stop  = trade.get("stop","?")
    tp    = trade.get("target","?")
    opp   = trade.get("opp_score","?")

    msg = (
        f"<b>AEGIS TRADE FIRED</b>\n\n"
        f"<b>{side} {sym}</b>\n"
        f"Entry: <code>${entry}</code>\n"
        f"Stop: <code>${stop}</code>\n"
        f"Target: <code>${tp}</code>\n"
        f"Score: <b>{opp}/100</b>\n\n"
        f"<i>{commentary}</i>"
    )
    send_telegram(msg)

def alert_morning_brief(brief: str):
    """Sends the morning brief to Telegram."""
    msg = f"<b>AEGIS MORNING BRIEF</b>\n\n{brief}"
    send_telegram(msg)

def alert_circuit_breaker(reason: str):
    """Emergency alert when circuit breaker fires."""
    msg = (
        f"<b>AEGIS CIRCUIT BREAKER FIRED</b>\n\n"
        f"Reason: {reason}\n\n"
        f"All trading halted. Review required."
    )
    send_telegram(msg)

# ─────────────────────────────────────────────────────
# INTERNAL LOG HELPER
# ─────────────────────────────────────────────────────
def _append_log(entry: dict):
    """Appends a JSON entry to the AI log file."""
    try:
        logs = []
        if os.path.isfile(AI_LOG_FILE):
            with open(AI_LOG_FILE, "r", encoding="utf-8") as f:
                try:
                    logs = json.load(f)
                except Exception:
                    logs = []
        logs.append(entry)
        # Keep last 500 entries
        if len(logs) > 500:
            logs = logs[-500:]
        with open(AI_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(logs, f, indent=2)
    except Exception as e:
        print(f"[AI] Log write failed: {e}")

# ─────────────────────────────────────────────────────
# STATUS CHECK
# ─────────────────────────────────────────────────────
def check_ai_status() -> dict:
    """Returns the current status of the AI module."""
    api_configured  = ANTHROPIC_API_KEY != "PASTE_YOUR_ANTHROPIC_API_KEY_HERE"
    tg_configured   = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    briefs_exist    = os.path.isfile(AI_BRIEF_FILE)
    briefs_count    = 0
    if briefs_exist:
        try:
            with open(AI_BRIEF_FILE, "r", encoding="utf-8") as f:
                briefs_count = f.read().count("Aegis AI Brief")
        except Exception:
            pass

    return {
        "api_configured":    api_configured,
        "telegram_configured": tg_configured,
        "model":             CLAUDE_MODEL,
        "briefs_generated":  briefs_count,
        "brief_file":        AI_BRIEF_FILE,
        "log_file":          AI_LOG_FILE
    }

