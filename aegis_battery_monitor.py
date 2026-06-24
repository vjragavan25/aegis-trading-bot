"""
Aegis Battery Monitor
=====================
Watches laptop battery and sends Telegram alerts at two thresholds:
  - LOW  (<= 20%, discharging) — plug in charger
  - HIGH (>= 80%, charging)    — safe to unplug

Run in a third terminal alongside server and bot:
    python aegis_battery_monitor.py
"""

import time
import psutil
import aegis_ai
from datetime import datetime, timezone

CHECK_INTERVAL  = 300   # check every 5 minutes
LOW_THRESHOLD   = 20    # alert when battery <= this % and discharging
HIGH_THRESHOLD  = 80    # alert when battery >= this % and charging
LOW_RESET_AT    = 25    # re-arm low alert once battery climbs back above this
HIGH_RESET_AT   = 75    # re-arm high alert once battery drops back below this


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def check_battery():
    b = psutil.sensors_battery()
    if b is None:
        return None, None, None
    return round(b.percent, 1), b.power_plugged, b.secsleft


def fmt_time_left(secsleft):
    if secsleft < 0:
        return ""
    h, m = divmod(secsleft // 60, 60)
    return f" (~{h}h {m}m remaining)" if h else f" (~{m}m remaining)"


def main():
    print("=" * 50)
    print("  AEGIS BATTERY MONITOR")
    print(f"  Low alert  : <= {LOW_THRESHOLD}% (discharging)")
    print(f"  High alert : >= {HIGH_THRESHOLD}% (charging)")
    print(f"  Check every: {CHECK_INTERVAL // 60} minutes")
    print("=" * 50)

    pct, plugged, secsleft = check_battery()
    if pct is None:
        print("  No battery detected — is this a desktop?")
        return

    print(f"  Current    : {pct}% | {'Charging' if plugged else 'Discharging'}")
    print("  Monitoring started. Press Ctrl+C to stop.\n")

    low_alert_sent  = False   # True while we're in a low-battery state
    high_alert_sent = False   # True while we're in a high-battery state

    while True:
        pct, plugged, secsleft = check_battery()

        if pct is None:
            time.sleep(CHECK_INTERVAL)
            continue

        status = "Charging" if plugged else "Discharging"
        print(f"[{now_utc()}] {pct}% | {status}")

        # LOW BATTERY alert — fires when discharging and at or below threshold
        if not plugged and pct <= LOW_THRESHOLD and not low_alert_sent:
            msg = (
                f"🔋 <b>AEGIS — Low Battery</b>\n"
                f"Battery at <b>{pct}%</b> and discharging"
                f"{fmt_time_left(secsleft)}.\n"
                f"Please plug in the charger to keep Aegis running."
            )
            sent = aegis_ai.send_telegram(msg)
            print(f"  ⚠ LOW BATTERY alert sent ({pct}%) — Telegram: {'OK' if sent else 'FAILED'}")
            low_alert_sent = True

        # Re-arm low alert once battery recovers above reset threshold
        if pct > LOW_RESET_AT:
            low_alert_sent = False

        # HIGH BATTERY alert — fires when charging and at or above threshold
        if plugged and pct >= HIGH_THRESHOLD and not high_alert_sent:
            msg = (
                f"🔌 <b>AEGIS — Battery Charged</b>\n"
                f"Battery at <b>{pct}%</b>.\n"
                f"Safe to unplug the charger to protect battery life."
            )
            sent = aegis_ai.send_telegram(msg)
            print(f"  ✓ HIGH BATTERY alert sent ({pct}%) — Telegram: {'OK' if sent else 'FAILED'}")
            high_alert_sent = True

        # Re-arm high alert once battery drops below reset threshold
        if pct < HIGH_RESET_AT:
            high_alert_sent = False

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Battery monitor stopped.")
