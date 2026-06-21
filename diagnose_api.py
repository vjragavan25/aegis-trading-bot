"""
Aegis — API Key Diagnostic Tool
=================================
Tests your Binance Demo API key against authenticated endpoints
to identify exactly why /account returns 401.

Run: python diagnose_api.py
"""

import hmac
import hashlib
import time
import json
import urllib.request
import urllib.error
import urllib.parse

# ─────────────────────────────────────────
# PASTE YOUR DEMO API KEYS HERE
# (same ones from aegis_server.py)
# ─────────────────────────────────────────
API_KEY    = "AzOs6WgkulFSQLlEvv5PtpVRhhbnDxlaXUcr8cpybvPHVGtQOPfY7oAynSspjh5x"
SECRET_KEY = "wZVC7EjUvniqnlfspx0B90fteGd1zo9tZ8EuggfQjfmibBvr8OWMRP4rNuHAAMvR"

REST_BASE = "https://demo-api.binance.com/api/v3"  # Updated: new Binance Demo Trading endpoint


def sign(params: dict) -> str:
    query = urllib.parse.urlencode(params)
    signature = hmac.new(
        SECRET_KEY.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return query + "&signature=" + signature


def test_public_endpoint():
    """Test 1: Public endpoint — no auth needed. Should always work."""
    print("\n[TEST 1] Public endpoint — GET /time (no auth)")
    try:
        with urllib.request.urlopen(f"{REST_BASE}/time", timeout=10) as r:
            data = json.loads(r.read())
            print(f"  ✓ PASS — server time: {data['serverTime']}")
            return True
    except Exception as e:
        print(f"  ✗ FAIL — {e}")
        print("  → Network/connectivity issue. Check internet connection.")
        return False


def test_api_key_header():
    """Test 2: Endpoint requiring API key header but NOT signature."""
    print("\n[TEST 2] API key header — GET /openOrders without signature")
    print("         (this should fail with -2014 'API-key format invalid'")
    print("          if key is malformed, or -1102 if missing params)")
    url = f"{REST_BASE}/openOrders"
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            print(f"  ? Unexpected success: {data}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  Response: {e.code} — {body}")
        if e.code == 401 and "-2015" in body:
            print("  → This is EXPECTED here (missing timestamp/signature)")
            print("  → If your key were malformed, you'd see -2014 instead")
            print("  → -2015 specifically means: key+signature mismatch OR")
            print("    permissions issue OR IP restriction — investigate further")
        return body


def test_signed_endpoint():
    """Test 3: Fully signed request — the real test."""
    print("\n[TEST 3] Signed endpoint — GET /account (full HMAC signature)")
    params = {"timestamp": int(time.time() * 1000)}
    query = sign(params)
    url = f"{REST_BASE}/account?{query}"
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            print(f"  ✓ PASS — Account data retrieved successfully!")
            print(f"  ✓ Can trade: {data.get('canTrade')}")
            balances = [b for b in data.get('balances', []) if float(b['free']) > 0]
            print(f"  ✓ Non-zero balances: {len(balances)}")
            for b in balances[:5]:
                print(f"      {b['asset']}: {b['free']}")
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  ✗ FAIL — HTTP {e.code}: {body}")
        diagnose_401(body)
        return False
    except Exception as e:
        print(f"  ✗ FAIL — {e}")
        return False


def diagnose_401(body):
    """Provide specific guidance based on the error body."""
    print("\n  --- DIAGNOSIS ---")
    if "-2015" in body:
        print("""
  Error -2015 means ONE of these (Binance doesn't tell us which):

  1. API KEY / SECRET MISMATCH
     → The API_KEY and SECRET_KEY you're using don't belong to the
       same key pair. Double-check you copied BOTH from the SAME
       key creation screen — not mixing an old key with a new secret.

  2. KEY WAS CREATED ON LIVE BINANCE, NOT DEMO/TESTNET
     → Demo trading keys ONLY work on demo-api.binance.com
     → Live keys ONLY work on api.binance.com
     → These are NOT interchangeable. A live key here = 401.

  3. KEY HAS EXPIRED OR TESTNET WAS RESET
     → Binance testnet periodically resets ALL data and invalidates
       ALL existing API keys without notice (this happens every
       few weeks/months). If your key worked before but stopped
       suddenly, this is the most likely cause.
     → FIX: Create a NEW API key on demo.binance.com right now

  4. IP RESTRICTION ENABLED ON THE KEY
     → If you set an IP whitelist when creating the key, and your
       IP has changed (common with home internet / mobile networks),
       all requests will be rejected.
     → FIX: Edit the key restrictions, set to Unrestricted (Demo OK)

  5. INCORRECT PERMISSIONS
     → Key needs "Enable Reading" to call /account
     → Verify in API Management that Reading is enabled
""")
    elif "-2014" in body:
        print("""
  Error -2014: API-key format invalid
  → The API_KEY string itself is malformed (wrong length, extra
    characters, or copy-paste included whitespace/newline)
  → FIX: Re-copy the key carefully, check for trailing spaces
""")
    elif "-1021" in body:
        print("""
  Error -1021: Timestamp outside recvWindow
  → Your computer's clock is out of sync with Binance servers
  → FIX: Sync your Windows clock (Settings > Time & Language)
""")
    else:
        print(f"  → Unrecognized error pattern. Body: {body}")


# ── RUN ALL TESTS ──────────────────────────────────────
print("=" * 60)
print("  Aegis API Diagnostic")
print("=" * 60)

if API_KEY == "PASTE_YOUR_DEMO_API_KEY_HERE":
    print("\n  ✗ Please paste your API_KEY and SECRET_KEY into this")
    print("    file (lines 16-17) before running.")
else:
    print(f"\n  Testing key: {API_KEY[:8]}...{API_KEY[-4:]}")
    print(f"  Against    : {REST_BASE}")

    if test_public_endpoint():
        test_api_key_header()
        success = test_signed_endpoint()

        print("\n" + "=" * 60)
        if success:
            print("  ✓ RESULT: Your API key is working correctly!")
            print("  → The 401 errors in your bot may be intermittent.")
            print("  → If this passes but the bot still fails, the issue")
            print("    may be in how aegis_server.py constructs the request.")
        else:
            print("  ✗ RESULT: Authentication is failing.")
            print("  → Most likely fix: CREATE A NEW API KEY on")
            print("    demo.binance.com (demo platform may periodically reset)")
            print("  → Then update BOTH aegis_server.py (lines 21-22)")
            print("    AND this file with the new key+secret")
        print("=" * 60)

print()
