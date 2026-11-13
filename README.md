# AI paper-trading test

A 3-month test of one question: **does Claude, deciding alone, beat simply buying and holding
the S&P 500 (SPY) once every cost is counted?**

Everything runs on its own in the cloud. It uses **simulated money only**: the code only knows
Alpaca's paper-trading address and cannot place a real trade.

## How it works (version 2, since 2026-11-13)

1. Every US trading day at 16:30 UTC (17:30 Paris in winter, 18:30 in summer), GitHub starts the run.
   It always records prices and the portfolio value and checks the loss stop (free).
2. **Claude decides when to decide.** With each decision it picks its next review, 1 to 10 trading
   days later. Code wakes it earlier only if markets jolt: SPY moves more than 2% in a day, a held
   ETF moves more than 5%, or the portfolio falls 5% since the last decision. Other days cost nothing.
3. When woken up, Claude gets:
   - prices of 8 ETFs (US stocks, tech, small companies, Europe/Japan, emerging markets, two kinds
     of US government bonds, gold): returns, volatility, trend and strength against SPY;
   - economic data from FRED (free): interest rates, yield curve, Fed rate, inflation,
     unemployment, credit spreads, VIX;
   - a news briefing that a separate Claude call writes from up to 5 web searches;
   - a scorecard of its past decisions: what each earned against simply keeping the previous holdings.
4. **Three independent answers, median used.** Claude (Opus 5.5, high effort) answers three times
   with the same information; the code takes the median share of each ETF. One odd answer cannot
   swing the portfolio, which also means fewer trades and fewer fees.
5. The code enforces the rules, whatever Claude says: no borrowing, no betting on falls, at most
   50% in one ETF, changes under 5% ignored, and **everything sold and the test stopped if the
   value falls 15% from its peak**.
6. Orders go to the Alpaca paper account. The ledger (`state/ledger.json`) and the
   results page (`report.md`) are saved in the GitHub project after each run.

## Money: what it costs and how it is counted

- **Claude API:** each call's real cost is measured from the API's own token counts, plus $0.01 per
  web search. Expected about $0.40 to $0.80 each time Claude is woken up (research + three
  answers), so roughly $3 to $6 per month at one or two decisions a week.
- **Hard ceiling:** before any call the code checks that research plus three answers, each at the
  most expensive possible size, would stay inside the test budget of **$20** (`API_BUDGET_USD`). Once reached, Claude is no longer
  called and the portfolio is simply held. The budget can never be set above your $200 credit.
- **Trading fees:** Alpaca's paper account charges nothing, which is unrealistic. The report
  charges every order as a small European investor would pay: **$1 per order + 0.1%** of the
  amount (spread and currency exchange), restated at a real size of **$1,000**.
- **Result shown:** gain after trading fees, API cost and any other cost, at $1,000, next to the
  same $1,000 bought once in SPY and held. Both sides ignore dividends.

All these numbers are in [bot/config.py](bot/config.py).

## Fair comparison: simple rules that cost nothing

On the same days, prices and fees, the code also runs three portfolios that need no AI:
60% SPY / 40% IEF; equal weight in the 8 ETFs, rebalanced monthly; and a trend rule (hold, in equal
shares, each ETF above its 200-day average). The dashboard ranks Claude against them and SPY.
If Claude cannot beat rules anyone could follow for free, it adds nothing.

## What to expect, honestly

A 2026 study that rebuilt published AI trading agents with costs and look-ahead controlled
("The Alpha Illusion", arXiv 2605.16895) found they trailed buy-and-hold on 4 of 5 stocks.
Version 2 gives Claude better information and fewer, steadier, cheaper decisions. It cannot
guarantee a win, and 3 months is short: a result either way can be luck.

## Setup (about 30 minutes, once)

1. **Alpaca paper account**
   - Open https://app.alpaca.markets/signup, fill in the form with your email and confirm it.
     Alpaca: "Anyone globally can create an Alpaca Paper Only Account" with just an email.
   - Log in at https://app.alpaca.markets/login.
   - Top-left corner: click the account selector and choose the **Paper Trading** account.
   - On the **Home** page, find the **API Keys** box and click **Generate** (or **Regenerate**).
   - Copy both values into a password manager or note: **API Key ID** and **Secret Key**.
     The secret is shown only once. Regenerating later cancels the old keys.
2. **Anthropic API key and spend limit**
   - Open https://platform.claude.com/settings/keys and sign in.
   - Click **Create key**, name it `paper-trading`, keep the defaults, confirm.
   - Copy the key (starts with `sk-ant-`). It is shown only once.
   - Open https://platform.claude.com/settings/spend-limits, click **Set limit** (or **Adjust
     limit**) next to *Monthly spend limit*, enter `25`, save. This caps all API use of your account.
3. **GitHub**: create a free account if you have none and a **private** repository. Upload this
   folder to it (I can do this for you once the repository exists).
4. In the repository: *Settings* > *Secrets and variables* > *Actions* > *New repository secret*,
   three times: `ANTHROPIC_API_KEY`, `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`.
5. Start it once by hand, while the US market is open (15:30-22:00 Paris time):
   *Actions* > *daily-run* > *Run workflow*. Then it runs alone every trading day.

GitHub emails you if a run fails. GitHub may pause scheduled runs in a project with no activity for
60 days; the daily commits should prevent this, and if GitHub ever emails about it, click to keep it on.

## Reading the results

Live dashboard: https://lucascverissim0.github.io/ai-paper-trading/

- **Overview:** value of $1,000 against SPY and the simple rules, fall from the peak, costs, holdings.
- **Positions & trades:** open positions with their P&L and today's move, today's value curve, every
  trade with its price, fee, realised P&L and the reason of that day's decision.
- **Performance:** return, volatility, Sharpe and Sortino ratios (against the 3-month Treasury bill
  rate), worst fall, best and worst day, beta and correlation to SPY, for the AI and every comparison;
  and, for each decision, whether it did better than keeping the previous holdings.
- **News & decisions:** for each decision, what changed, the news Claude read with its sources, the
  economic data, the facts that drove it (news, economy, prices, portfolio or costs, and which ETFs it
  raised or lowered), and its three answers. Plus the latest free headlines on the 8 ETFs.

Decision data updates after the daily run; live positions, P&L and headlines about every 15 minutes
while the US market is open (a separate free workflow, `live-refresh`, that reads the account and
never trades). GitHub's timer can run a few minutes late.

Or open `report.md` in the repository. The line that matters: **"the AI is AHEAD of / BEHIND
buy-and-hold by $X after all costs"**.

## Before any real money (rule decided now, before seeing results)

- Only consider it if, after **at least 3 months**, the AI is ahead after all costs **and** its
  worst fall is smaller than SPY's. Even then, 3 months can be luck; 12 months is far more telling.
- Placing real trades stays your decision and your action: this code cannot do it, and I will not
  convert it to do so.
- In the EU, private investors usually cannot buy US-listed ETFs like SPY; real-money equivalents
  would be European-listed (UCITS) versions, with their own fees.

## Run the offline tests

```bash
python3 -m unittest tests.test_bot
```
