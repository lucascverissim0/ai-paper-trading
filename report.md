# AI paper-trading test - results (version 3: stock picking)

Last update: 2026-11-19 - status: running

Simulated money only. No real money is used or at risk.

Results restated as if $1,000 had been invested.

| | AI strategy | Buy and hold SPY |
|---|---|---|
| Return before costs | -1.44% | -4.40% |
| Trading fees (modelled) | $10.63 | $2.00 |
| Claude API cost (measured) | $1.66 | $0.00 |
| Other costs | $0.00 | $0.00 |
| Gain after all costs | -$26.67 (-2.67%) | -$45.99 (-4.60%) |
| Worst fall from a peak | -1.44% | -4.40% |

**Verdict so far: the AI is AHEAD of buy-and-hold by $19.32 after all costs.**

Days since start: 6 - AI decisions made: 2 - API budget used: $1.66 of $60.00.

Stop-losses that sold a position between runs: 2.

Under 12 months, a result either way can be luck. Dividends are left out on both sides.

Simple rules on the same days, prices and fees (no AI cost):

| Rule | Gain after fees |
|---|---|
| Buy and hold QQQ (Nasdaq-100) | -$2.98 (-0.30%) |
| 60% SPY / 40% IEF | -$12.12 (-1.21%) |
| Equal weight, 8 ETFs | -$26.55 (-2.66%) |
| Trend rule (above 200-day average) | -$28.63 (-2.86%) |

Last decision (2026-11-19): APPN 11%, HAE 10%.

Reason given: The market is in a correction with a hawkish Fed, so I keep about 79% in cash and hold only the two leaders that are acting well, with tight stops. APPN's stop moves up slightly and HAE's stop stays at 115. New buys wait until the indexes reclaim their 50-day averages and breadth improves.

Stop-losses in place: APPN at 51.50, HAE at 115.00.
