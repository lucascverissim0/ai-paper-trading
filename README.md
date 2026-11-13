# AI paper-trading test

A 3-month test of one question: **does Claude, deciding alone every trading day, beat simply
buying and holding the S&P 500 (SPY) once every cost is counted?**

Everything runs on its own in the cloud. It uses **simulated money only**: the code only knows
Alpaca's paper-trading address and cannot place a real trade.

## How it works

1. Every US trading day at 16:30 UTC (17:30 Paris in winter, 18:30 in summer), GitHub starts the run.
2. The code reads prices for 8 ETFs (US stocks, tech, small companies, Europe/Japan, emerging
   markets, two kinds of US government bonds, gold) and the current paper portfolio.
3. Claude (Opus 5.5) gets the figures and answers with the target share for each ETF.
4. The code enforces the rules, whatever Claude says: no borrowing, no betting on falls, at most
   50% in one ETF, changes under 5% ignored, and **everything sold and the test stopped if the
   value falls 15% from its peak**.
5. Orders go to the Alpaca paper account. The ledger (`state/ledger.json`) and the
   results page (`report.md`) are saved in the GitHub project after each run.

## Money: what it costs and how it is counted

- **Claude API:** each decision's real cost is measured from the API's own token counts. Expected
  about $0.03 to $0.07 per decision, so roughly $1 to $1.50 per month.
- **Hard ceiling:** before each call the code checks that even the most expensive possible answer
  would stay inside the test budget of **$20** (`API_BUDGET_USD`). Once reached, Claude is no longer
  called and the portfolio is simply held. The budget can never be set above your $200 credit.
- **Trading fees:** Alpaca's paper account charges nothing, which is unrealistic. The report
  charges every order as a small European investor would pay: **$1 per order + 0.1%** of the
  amount (spread and currency exchange), restated at a real size of **$1,000**.
- **Result shown:** gain after trading fees, API cost and any other cost, at $1,000, next to the
  same $1,000 bought once in SPY and held. Both sides ignore dividends.

All these numbers are in [bot/config.py](bot/config.py).

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

Open `report.md` in the repository. The line that matters: **"the AI is AHEAD of / BEHIND
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
