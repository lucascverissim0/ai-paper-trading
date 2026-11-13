# AI paper-trading test

A forward test of one question: **can Claude, picking individual stocks, beat simply buying and
holding the S&P 500 (SPY) once every cost is counted?** The owner's target is +100% a year.

Everything runs on its own in the cloud. It uses **simulated money only**: the code only knows
Alpaca's paper-trading address and cannot place a real trade.

## What to expect, honestly (read first)

- No public, audited strategy makes 100% a year reliably. The best verified long record, Renaissance
  Technologies' Medallion fund, averaged about 66% a year before fees and 39% after (1988-2018,
  reconstructed by its biographer); it is closed to outsiders and trades at high frequency.
- The verified people who made 100%+ in a year are real-money contest winners trading **momentum
  growth stocks**: Mark Minervini won the US Investing Championship with +155% (1997) and +334.8%
  (2021). Kristjan Kullamagi ("Qullamaggie"), a breakout trader, reports turning a few thousand dollars
  into tens of millions; that is self-reported, not audited. Their common method: buy the market's
  strongest stocks as they break out near new highs, cut each loser fast with a stop-loss, let
  winners run, concentrate in a few names, step aside when the market is weak. About 30% of their
  trades win; the few big winners pay for everything.
- Version 3 automates the closest version of that method. +100% is the target, not a promise. A
  concentrated stock portfolio can also fall 30-50%. A 2026 study that rebuilt published AI trading
  agents with costs and look-ahead controlled ("The Alpha Illusion", arXiv 2605.16895) found they
  trailed buy-and-hold on 4 of 5 stocks. Judge after months, not weeks.
- No backtest is possible: Claude already knows past market moves, so only this forward test counts.

## How it works (version 3, since 2026-11-13)

Every US trading day at 16:30 UTC (17:30 Paris in winter, 18:30 in summer), GitHub starts the run:

1. **Screen (code, free).** Every active, tradable US-listed stock, ETF and ADR (foreign companies
   listed in the US) on NYSE, Nasdaq, NYSE Arca, NYSE American and Cboe, about 8,000 symbols. It keeps
   the liquid ones (price at least $5, at least $20M traded a day) in the "trend template" of momentum
   traders: price above rising 50, 150 and 200-day averages, within 25% of the 52-week high, at least
   30% above the 52-week low, and stronger than 70% of liquid stocks over 3-12 months. Leveraged funds
   are left out of the screen. The 40 strongest become the day's candidates. It also measures the market
   backdrop: SPY, QQQ and IWM against their averages, and the share of stocks in an uptrend.
2. **Analyst (Claude Haiku 5.5, cheap).** For each new candidate and each stock held, it reads the
   price figures and the last 15 headlines (free Alpaca news) and writes a short card: business,
   growth facts, catalysts, risks, next earnings date if stated, and a setup score from 1 to 10.
   Cards are reused for 7 days, so most days only new candidates cost anything.
3. **Market briefing (Claude Opus 5.5 + web search).** Up to 5 searches on the first day, 3 after:
   market direction, Fed and rates, earnings calendar, leading groups, news on holdings and top
   candidates. Plus free economic data from FRED (rates, yield curve, inflation, unemployment, credit
   spreads, VIX).
4. **Decision (Claude Opus 5.5, three independent answers).** It sees all of the above, its positions
   with gains and stops, and a scorecard of its past decisions. Each answer lists the stocks to hold,
   their weight, a stop-loss price and a reason. **A stock is held only if most answers hold it**, at the
   median weight. It may also name any tradable stock that is not a candidate (e.g. a stock it reads
   about in the news).
5. **Orders and stop-losses.** Sells first, then buys in whole shares with the cash available (never
   borrows). Then every position gets a stop-loss order at Alpaca that sells it automatically during
   market hours, between runs: Claude's price if usable, otherwise 8% below the price. **A stop is never
   lowered**, only raised.
6. The ledger (`state/ledger.json`), `report.md` and the dashboard data are saved in the repository.

Rules enforced by code: long only, no borrowing, no shorting, changes under 1% of the portfolio
ignored, a stop-loss under every position. Lucas chose **no loss limit on the whole test and no cap
per stock**. Stopped if the API budget is reached (then positions and stops simply stay).

## Money: what it costs and how it is counted

- **Claude API:** each call's real cost is measured from the API's own token counts, plus $0.01 per
  web search. Expected about $0.60 to $1.00 per trading day (analyst cards a few cents, research about
  $0.30, three decisions about $0.40-0.60), so about $15 to $20 a month.
- **Hard ceiling:** before any call the code checks that every call of the day, each at its most
  expensive possible size, would stay inside the test budget of **$60** (`API_BUDGET_USD`, Lucas's
  choice). The budget can never be set above the $200 credit.
- **Trading fees:** Alpaca's paper account charges nothing, which is unrealistic. The report charges
  every order, stop-loss sales included, as a small European investor would pay: **$1 per order + 0.1%**,
  restated at a real size of **$1,000**. With 5-10 stocks, a $1 fee is about 0.7% per order: frequent
  trading is expensive at this size.
- **Result shown:** gain after trading fees and API cost at $1,000, next to the same $1,000 bought
  once in SPY and held. Both sides ignore dividends.

All these numbers are in [bot/config.py](bot/config.py).

## Fair comparison: simple rules that cost nothing

On the same days, prices and fees, the code also runs four portfolios that need no AI: buy and hold
QQQ (tech); 60% SPY / 40% IEF; equal weight in 8 ETFs, rebalanced monthly; and a trend rule over those
8 ETFs. If Claude cannot beat rules anyone could follow for free, it adds nothing.

## History

- Version 1-2 (2026-11-13): Claude chose weights among 8 ETFs. Replaced the same day by version 3 at
  Lucas's request; its ledger is kept in `state/archive/etf-test-2026-11.json`.

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
- **Positions** also shows each stop-loss and today's candidates with their analyst cards.
- **Decisions & news:** for each day, what changed and why per stock, the stops set, stop-losses that
  sold, the news Claude read with its sources, the economic data, the facts that drove it, and its three
  answers. Plus the latest free headlines on the stocks held.

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
- In the EU, private investors can usually buy US stocks but not US-listed ETFs like SPY.

## Run the offline tests

```bash
python3 -m unittest tests.test_bot
```
