# Aegis Reversal Short Engine — Observation Log

**Strategy:** Detect reversal signals on top-performing assets, log outcomes.
**Started:** 2026-06-27
**Target:** 30-50 observations before strategy review

---

## OBSERVATION #1 — 2026-06-27 13:36 UTC

Asset          : VELVETUSDT
Entry price    : $1.4379
24h gain       : +130.0%
24h volume     : $597M

Signals present:
  [ ] RSI(1H) crossed below 70  (RSI: 87.6)
  [x] Volume declining 2+ consecutive candles
  [x] Funding rate > +0.06%  (rate: 0.070%)
  [ ] 1H close below 20-period SMA
  Signals count : 2/4

Market regime  : Strong bull
BTC regime     : Unknown

OUTCOME
  4H   : +25.5%  (reversal >3%: NO)
  24H  : +25.4%  (reversal >3%: NO)
  72H  : +11.1%  (reversal >3%: NO)  [estimated — file-write bug on 72H fill]
  Max reversal : +11.1%

Notes: No reversal across all 3 windows. Price continued up from entry ($1.4379) through peak (~$2.05) then pulled back; all outcome windows captured above-entry prices.
---

## OBSERVATION #2 — 2026-06-27 16:22 UTC

Asset          : WIFUSDT
Entry price    : $0.1772
24h gain       : +19.9%
24h volume     : $82M

Signals present:
  [x] RSI(1H) crossed below 70  (RSI: 69.1)
  [x] Volume declining 2+ consecutive candles
  [ ] Funding rate > +0.06%  (rate: 0.005%)
  [ ] 1H close below 20-period SMA
  Signals count : 2/4

Market regime  : Strong bull
BTC regime     : Unknown

OUTCOME
  4H   : -4.6%  (reversal >3%: YES)
  24H  : -1.8%  (reversal >3%: NO)
  72H  : [PENDING — check 2026-06-30 16:22 UTC]
  Max reversal : [PENDING]

Notes: 
---

## OBSERVATION #3 — 2026-06-28 04:39 UTC

Asset          : PIEVERSEUSDT
Entry price    : $0.7931
24h gain       : +16.6%
24h volume     : $51M

Signals present:
  [x] RSI(1H) crossed below 70  (RSI: 52.8)
  [ ] Volume declining 2+ consecutive candles
  [ ] Funding rate > +0.06%  (rate: 0.020%)
  [x] 1H close below 20-period SMA
  Signals count : 2/4

Market regime  : Sideways
BTC regime     : Unknown

OUTCOME
  4H   : +0.4%  (reversal >3%: NO)
  24H  : -3.7%  (reversal >3%: YES)  [estimated — watcher was offline]
  72H  : [PENDING — check 2026-07-01 04:39 UTC]
  Max reversal : [PENDING]

Notes: 
---

## OBSERVATION #4 — 2026-06-29 02:30 UTC

Asset          : RAVEUSDT
Entry price    : $0.3368
24h gain       : +32.7%
24h volume     : $89M

Signals present:
  [x] RSI(1H) crossed below 70  (RSI: 69.7)
  [x] Volume declining 2+ consecutive candles
  [ ] Funding rate > +0.06%  (rate: 0.005%)
  [ ] 1H close below 20-period SMA
  Signals count : 2/4

Market regime  : Strong bull
BTC regime     : Unknown

OUTCOME
  4H   : +26.4%  (reversal >3%: NO)  [estimated — watcher was offline]
  24H  : +5.7%  (reversal >3%: NO)
  72H  : [PENDING — check 2026-07-02 02:30 UTC]
  Max reversal : [PENDING]

Notes: 
---

## OBSERVATION #5 — 2026-06-29 05:01 UTC

Asset          : SLXUSDT
Entry price    : $0.6327
24h gain       : +21.2%
24h volume     : $244M

Signals present:
  [x] RSI(1H) crossed below 70  (RSI: 69.8)
  [x] Volume declining 2+ consecutive candles
  [ ] Funding rate > +0.06%  (rate: 0.013%)
  [ ] 1H close below 20-period SMA
  Signals count : 2/4

Market regime  : Strong bull
BTC regime     : Unknown

OUTCOME
  4H   : -12.6%  (reversal >3%: YES)  [estimated — watcher was offline]
  24H  : -22.8%  (reversal >3%: YES)
  72H  : [PENDING — check 2026-07-02 05:01 UTC]
  Max reversal : [PENDING]

Notes: 
---

## OBSERVATION #6 — 2026-06-30 02:18 UTC

Asset          : UBUSDT
Gain tier      : HIGH
Entry price    : $0.1114  (price when observation logged)
Peak price     : $0.1251  (tracked peak at observation time)
Peak-to-entry  : -11.0%  (how far price had already fallen from peak)
24h gain       : +36.7%
24h volume     : $61M
ATR(14) at entry: $0.0061
Support level  : Not identified
Support tests  : N/A

Signals present:
  [x] RSI(1H) crossed below 70  (RSI: 55.0)
  [ ] Volume declining 2+ consecutive candles
  [ ] Funding rate > +0.06%  (rate: 0.055%)
  [x] 1H close below 20-period SMA
  Signals count : 2/4

BTC regime     : Sideways
Market regime  : Sideways

OUTCOME
  4H   : +8.9%  (reversal >3%: NO)
  24H  : [PENDING — check 2026-07-01 02:18 UTC]
  72H  : [PENDING — check 2026-07-03 02:18 UTC]
  Max reversal : [PENDING]

Notes:
---

