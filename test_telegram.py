"""
Aegis — Telegram Connection Test
==================================
1. Paste your Bot Token and Chat ID below
2. Run: python test_telegram.py
3. You should receive "Aegis AI is online" on your phone

How to get your Bot Token:
  - Open Telegram → search @BotFather
  - Send /mybots → select your bot → API Token → copy it

How to get your Chat ID:
  - Send any message to your bot first
  - Open in browser: https://api.telegram.org/botYOUR_TOKEN/getUpdates
  - Find "chat": { "id": 123456789 } — that number is your Chat ID
"""

import urllib.request
import json

# ─────────────────────────────────────────
# PASTE YOUR DETAILS HERE
# ─────────────────────────────────────────
TOKEN   = "8906203493:AAGIqwO-oliuOE20rvCgcEbpJJbHEztHNGQ"
CHAT_ID = "8876022187"
# ─────────────────────────────────────────


def test_token():
    """Step 1 — verify the token is valid"""
    print("\n[1] Verifying bot token...")
    url = f"https://api.telegram.org/bot{TOKEN}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            result = json.loads(r.read())
            if result.get("ok"):
                bot = result["result"]
                print(f"    ✓ Token valid")
                print(f"    ✓ Bot name    : {bot.get('first_name')}")
                print(f"    ✓ Bot username: @{bot.get('username')}")
                return True
            else:
                print(f"    ✗ Token invalid: {result}")
                return False
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"    ✗ HTTP {e.code}: {body}")
        return False
    except Exception as e:
        print(f"    ✗ Connection error: {e}")
        return False


def test_send_message():
    """Step 2 — send a test message"""
    print("\n[2] Sending test message...")
    url  = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    body = json.dumps({
        "chat_id":    CHAT_ID,
        "text":       "✅ Aegis AI is online\n\nTelegram alerts are working correctly.",
        "parse_mode": "HTML"
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            if result.get("ok"):
                chat = result["result"]["chat"]
                name = chat.get("first_name") or chat.get("title") or "unknown"
                print(f"    ✓ Message delivered to: {name}")
                print(f"    ✓ Chat ID confirmed: {chat.get('id')}")
                return True
            else:
                print(f"    ✗ Send failed: {result}")
                return False
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"    ✗ HTTP {e.code}: {body}")
        if e.code == 400:
            print("    → Chat ID is wrong or you haven't sent a message to the bot yet")
            print("    → Send any message to your bot on Telegram first, then retry")
        if e.code == 401:
            print("    → Bot token is invalid — get fresh token from @BotFather")
        return False
    except Exception as e:
        print(f"    ✗ Error: {e}")
        return False


def get_chat_id():
    """Helper — fetch your Chat ID automatically"""
    print("\n[3] Auto-detecting your Chat ID...")
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            result = json.loads(r.read())
            updates = result.get("result", [])
            if not updates:
                print("    ⚠ No messages found.")
                print("    → Send any message to your bot on Telegram, then run again.")
                return None
            latest = updates[-1]
            chat   = latest.get("message", {}).get("chat", {})
            cid    = chat.get("id")
            name   = chat.get("first_name") or chat.get("title") or "unknown"
            print(f"    ✓ Found Chat ID : {cid}")
            print(f"    ✓ From          : {name}")
            print(f"\n    → Paste this into aegis_ai.py line 36:")
            print(f'      TELEGRAM_CHAT_ID = "{cid}"')
            return str(cid)
    except Exception as e:
        print(f"    ✗ Error: {e}")
        return None


# ── RUN TESTS ──────────────────────────────────────
print("=" * 50)
print("  Aegis — Telegram Connection Test")
print("=" * 50)

if TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
    print("\n  ✗ TOKEN not set.")
    print("  Open this file in Notepad and paste your bot token on line 17.")
else:
    token_ok = test_token()

    if token_ok:
        if CHAT_ID == "PASTE_YOUR_CHAT_ID_HERE":
            print("\n  ⚠ CHAT_ID not set — attempting auto-detect...")
            detected = get_chat_id()
            if detected:
                print(f"\n  Re-run after setting CHAT_ID = \"{detected}\" on line 18")
        else:
            msg_ok = test_send_message()

            if token_ok and msg_ok:
                print("\n" + "=" * 50)
                print("  ✓ All tests passed!")
                print("  ✓ Paste these into aegis_ai.py:")
                print(f'    TELEGRAM_BOT_TOKEN = "{TOKEN}"')
                print(f'    TELEGRAM_CHAT_ID   = "{CHAT_ID}"')
                print("=" * 50)
            else:
                print("\n  ✗ Some tests failed — see messages above.")

print()
