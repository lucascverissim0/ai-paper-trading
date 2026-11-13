# AI paper-trading test - results

Last update: 2026-11-13 - status: running

Simulated money only. No real money is used or at risk.

Results restated as if $1,000 had been invested.

| | AI strategy | Buy and hold SPY |
|---|---|---|
| Return before costs | -0.46% | -0.49% |
| Trading fees (modelled) | $3.95 | $2.00 |
| Claude API cost (measured) | $0.66 | $0.00 |
| Other costs | $0.00 | $0.00 |
| Gain after all costs | -$9.24 (-0.92%) | -$6.94 (-0.69%) |
| Worst fall from a peak | -0.46% | -0.49% |

**Verdict so far: the AI is BEHIND buy-and-hold by $2.30 after all costs.**

Days since start: 0 - AI decisions made: 1 - API budget used: $0.66 of $20.00.

Under 12 months, a result either way can be luck. Dividends are left out on both sides.

Simple rules on the same days, prices and fees (no AI cost):

| Rule | Gain after fees |
|---|---|
| 60% SPY / 40% IEF | -$3.00 (-0.30%) |
| Equal weight, 8 ETFs | -$8.99 (-0.90%) |
| Trend rule (above 200-day average) | -$5.00 (-0.50%) |

Next review chosen by the AI: 2026-11-27 (earlier if markets jolt).

Last decision (2026-11-13): EFA 25%, QQQ 20%, SPY 50%.

Reason given: The benchmark is SPY, so I hold the maximum 50% in SPY and fill the rest with QQQ and EFA, which track SPY closely, using only three orders to keep costs down. I am avoiding bonds and gold because rate-hike expectations are pushing yields up and gold is in a strong downtrend, and EEM is weak against a backdrop of rising US yields. QQQ is kept below its natural weight because of the Nvidia turmoil, and the long review interval limits API and trading costs.
