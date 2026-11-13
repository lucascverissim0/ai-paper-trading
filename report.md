# AI paper-trading test - results (version 3: stock picking)

Last update: 2026-11-13 - status: running

Simulated money only. No real money is used or at risk.

Results restated as if $1,000 had been invested.

| | AI strategy | Buy and hold SPY |
|---|---|---|
| Return before costs | -0.16% | -0.06% |
| Trading fees (modelled) | $8.40 | $2.00 |
| Claude API cost (measured) | $1.03 | $0.00 |
| Other costs | $0.00 | $0.00 |
| Gain after all costs | -$11.04 (-1.10%) | -$2.57 (-0.26%) |
| Worst fall from a peak | -0.16% | -0.06% |

**Verdict so far: the AI is BEHIND buy-and-hold by $8.47 after all costs.**

Days since start: 0 - AI decisions made: 1 - API budget used: $1.03 of $60.00.

Stop-losses that sold a position between runs: 0.

Under 12 months, a result either way can be luck. Dividends are left out on both sides.

Simple rules on the same days, prices and fees (no AI cost):

| Rule | Gain after fees |
|---|---|
| Buy and hold QQQ (Nasdaq-100) | -$2.00 (-0.20%) |
| 60% SPY / 40% IEF | -$3.00 (-0.30%) |
| Equal weight, 8 ETFs | -$8.99 (-0.90%) |
| Trend rule (above 200-day average) | -$5.00 (-0.50%) |

Last decision (2026-11-13): ANDG 12%, HNGE 12%, APPN 11%, HAE 10%.

Reason given: The market is in a correction with QQQ below its 50-day average and a Fed hike looming, so I am swapping the index ETFs for about 45% in four leaders with tight bases near highs and holding the rest in cash. Each position is sized around 10-12% so costs stay small, and stops sit about 6-8% below the price, just under recent support. I will add exposure if the indexes recover their 50-day averages and these breakouts follow through.

Stop-losses in place: ANDG at 61.00, APPN at 50.90, HAE at 115.00, HNGE at 112.00.
