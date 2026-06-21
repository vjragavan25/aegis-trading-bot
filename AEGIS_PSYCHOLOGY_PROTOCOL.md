# AEGIS PSYCHOLOGY PROTOCOL
### Operator Decision Framework — Demo Phase

**Version:** 1.0  
**Established:** 2026-06-15  
**Last reviewed:** 2026-06-17  
**Status:** Active — applies from first trade through Live Readiness Gate

---

## Purpose

The biggest source of edge destruction in systematic trading is not bad signals, bad code, or bad markets. It is the operator overriding the system based on feelings in the moment — panic, greed, impatience, or the false confidence that comes from watching a position move.

This protocol exists to define exactly which decisions belong to the system and which belong to the operator, and to make violations explicit and reviewable rather than invisible.

The system makes entry and exit decisions. The operator maintains the system, reviews its performance, and decides when conditions warrant a pause or review. The operator does not override individual trade decisions.

---

## Rule 1 — No Override

**If the bot says WAIT, the operator never manually enters.**

The bot's entry gate exists because human judgment in real-time is reliably worse than a rules-based system with defined thresholds. The moment you override a WAIT signal — even if that trade would have won — you have introduced a discretionary layer that invalidates the entire statistical learning process. You can no longer tell whether the system has edge, because you are no longer running the system.

**What this covers:**
- Seeing a "nearly qualified" signal (Opp:66, gate requires 68) and manually entering
- Entering because the chart "looks good" even though scores failed
- Entering during a Bearish or Sideways regime because "this feels like a reversal"
- Entering a second position in the same asset because "the momentum is strong"

**What this does NOT cover:**
- Placing a manual protective SL on an existing unprotected position (this is safety action, not override)
- Manually closing an existing position after the system has already entered and a clear decision to exit has been made (this is management of an existing position, not a new entry decision)
- Resetting the circuit breaker after manual review (this is maintenance, not a trade decision)

**Violation cost in this project:**  
Not directly violated so far, but the two consecutive ETHUSDT entries on 2026-06-16 (which lost -$13.12 combined) were only possible because the concentration check (`MAX_OPEN_POSITIONS_PER_ASSET=1`) did not yet exist. The second entry was the system overriding itself. The fix is now deployed — this scenario is mechanically prevented, not just protocol-prevented.

---

## Rule 2 — No Panic

**Once a trade is open, the operator never manually moves or cancels the SL or TP.**

The SL and TP were calculated at entry time using the system's risk parameters. They represent the pre-committed answer to "what do I do if this goes wrong / right?" Touching them after the fact — especially moving the SL further away to "give it more room" — is the most common way systematic traders destroy their own edge.

**What this covers:**
- Moving the SL further away because the position is near the SL and "it will probably recover"
- Cancelling the TP early because you "want to lock in the profit" before it hits the target
- Widening the TP because "the momentum is so strong it will go further"
- Closing the position manually mid-trade because you are uncomfortable watching it move against you

**What this does NOT cover:**
- Closing a position manually when the system has tripped a circuit breaker and the OCO has failed (UNPROTECTED state — this is emergency intervention, not discretionary override)
- Closing a position manually when a clear structural reason exists that the system cannot see (e.g., knowledge of a major news event outside the system's data sources) — this should be rare, documented, and reviewed

**Violation cost in this project:**  
The ETH trade on 2026-06-15 was closed manually before hitting its TP ($1834.13). It closed at $1827.47, capturing +1.81R instead of the planned +2.2R. The system was right — price was approaching the TP — but the position was closed early due to time pressure (circuit breaker was active, wanted a clean slate for testing). This was a justified exception given the testing context. Under normal operations, this would be a Rule 2 violation.

---

## Rule 3 — Three-Loss Pause

**Three consecutive losses = 24-hour mandatory pause + structured review.**

Consecutive losses in a systematic strategy can mean one of three things: (1) bad luck in a regime the system is not designed for, (2) a genuine bug or degraded condition, or (3) a real deterioration in the system's edge. You cannot tell which it is in the moment. The pause creates space to find out.

The review is not optional and not a formality. It must answer:

1. Were the three losses taken in the correct regime (Strong Bull, consecutive cycles confirmed)?
2. Did the entries pass all five original gate conditions (Opp ≥68, Conf ≥65, Risk ≤50, Vol ≥1.2x, TF ≥2/3) plus the two new ones (regime_quality, regime_consistency)?
3. Did the SL fire correctly at the intended price (within 0.5% slippage)?
4. Is the current market regime different from the entry regime?
5. Is there a code or data issue that needs investigation?

If all five answers are "yes/normal" and the market has clearly shifted regime, the pause ends and trading resumes. If any answer is "no/unclear," the investigation continues until resolved.

**Current consecutive loss count:** 2 (trades #4 and #5, both OCO SL on 2026-06-16)

**Note:** The two consecutive losses on 2026-06-16 were both OCO-SL exits in a market that shifted from Strong Bull to Bearish within 60 minutes of entry. Both entries passed all gate conditions at the time. This is within expected system behavior — not a trigger for Rule 3 review, since the count is 2, not 3. One more consecutive loss would trigger the pause.

---

## Rule 4 — Weekly Review

**30 minutes every Sunday reviewing the prior week's signal log and trade outcomes.**

This is the mechanism by which the operator stays calibrated to the system's actual behavior rather than their narrative about it. Without structured review, humans drift toward selectively remembering wins and rationalizing losses, which creates false confidence and delays identification of real problems.

**Review agenda (30 minutes, every Sunday):**

**Part 1 — Trade outcomes (10 min)**
- How many trades fired this week?
- What were the entry conditions for each (regime, scores, volume)?
- Which exited via TP, which via SL, which via manual close?
- What is the rolling win rate (last 10 OCO-driven trades only — manual closes excluded)?
- Is the rolling win rate above or below 55%?

**Part 2 — Signal quality (10 min)**
- Open `aegis_signals.csv`, filter this week's rows
- How many TRADE signals fired vs WAIT?
- What was the most common failure reason for WAIT signals?
- Were there any TRADE signals that looked suspicious in hindsight (regime_consistency barely passed, volume marginally above threshold)?

**Part 3 — System health (10 min)**
- Any circuit breaker trips this week? Reason?
- Any OCO failures or fallbacks to SL-only?
- Any unexpected bot behavior (long gaps between cycles, fetch errors)?
- Any `[ERR]` lines in `aegis_events.txt` that weren't investigated?

**Output:** A one-paragraph written note (can be brief) added to a running `aegis_weekly_reviews.txt` file in the Run folder. Even three sentences counts. The act of writing forces clarity.

---

## Rule 5 — Live Gate Discipline

**No live trading discussion, planning, or preparation until the Live Readiness Gate is met.**

The Live Readiness Gate is:
- 30+ closed trades (OCO-driven exits only — manual closes do not count)
- Rolling win rate ≥55% (last 30 OCO-driven trades)
- Average R:R achieved ≥2.0:1
- Maximum drawdown <15%
- Zero unresolved circuit breaker trips in the last 10 trades

**Why this rule exists:**  
The pressure to "go live" builds quickly once a system appears to be working. A profitable trade, a streak of good signals, a feeling of confidence — all of these create urgency that is entirely disconnected from whether the system actually has a statistically valid edge. The gate numbers were chosen to require enough trades to make the win rate statistically meaningful (at 30 trades, a 55% win rate has a 90% confidence interval of roughly 37%–73% — still wide, but wide in the right direction). Until those numbers are met, the system is in hypothesis-testing mode, not deployment mode.

**The 60-day no-live-trading agreement:**  
Separately from the gate metrics, a 60-day moratorium on live trading was agreed from the date of AI integration (approximately 2026-06-13). This expires around 2026-08-12. Even if the gate metrics are met before that date, no live trading before 2026-08-12.

**Current gate status (as of 2026-06-17):**

| Metric | Required | Current | Status |
|--------|----------|---------|--------|
| Closed trades (OCO) | 30 | 2 | 🔴 Far below |
| Win rate (OCO trades) | ≥55% | 0% (0W/2L) | 🔴 Below |
| Avg R:R achieved | ≥2.0:1 | -1.06R | 🔴 Below |
| Max drawdown | <15% | ~0.13% | 🟢 Within limit |
| Unresolved CB trips | 0 | 0 | 🟢 Clean |

The gate is nowhere near met. This is expected at N=2 OCO trades. Keep running the system.

---

## What Counts as a Violation

A violation is any action that substitutes the operator's in-the-moment judgment for the system's pre-defined rules on a live trade decision. Violations must be logged, not hidden.

When a violation occurs:
1. Write a one-line entry in `aegis_weekly_reviews.txt`: date, which rule, what happened
2. Ask: "Would I do this again under the same conditions?" If yes, consider whether the rule should be updated. If no, note it as a lapse and continue
3. Do not adjust gate metrics to accommodate violations

Violations are not moral failures. They are data points. The protocol is designed to make them visible so they can be learned from, not to create shame that causes underreporting.

---

## Changes to This Document

This document should be reviewed and updated at the weekly Sunday review. Version history:

| Version | Date | Change |
|---------|------|--------|
| 1.0 | 2026-06-17 | Initial write — formalises 4 rules discussed in project log sessions, adds Rule 5 (Live Gate Discipline) |

Any change to a rule requires a note explaining why the rule was insufficient as written. Rules should become more precise over time, not looser.
