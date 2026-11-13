"""Every setting of the paper-trading test lives here.

Change numbers here (or through environment variables), never inside the logic.
"""
from __future__ import annotations

import os


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


# --- What the AI may buy ---------------------------------------------------------
# Any active, tradable US-listed stock, ETF or ADR (foreign companies listed in the US) on these
# exchanges. Long only: no shorting, no borrowing. Over-the-counter shares are left out.
EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}
BENCHMARK = "SPY"  # the AI must beat simply buying and holding this

# Fixed ETFs whose prices are recorded every day for the free rule portfolios (no AI) on the dashboard.
SHADOW_ETFS = {
    "SPY": "US large companies (S&P 500)",
    "QQQ": "US large tech-heavy companies (Nasdaq-100)",
    "IWM": "US small companies (Russell 2000)",
    "EFA": "Developed markets outside North America",
    "EEM": "Emerging markets",
    "TLT": "US government bonds, 20+ years",
    "IEF": "US government bonds, 7-10 years",
    "GLD": "Gold",
}

# --- Stock screen (code only, free) -------------------------------------------------
BARS_FEED = os.environ.get("BARS_FEED") or "sip"  # all US exchanges; falls back to "iex" if refused
SCREEN_CALENDAR_DAYS = 400       # history fetched: enough for 52-week and 200-day figures
SCREEN_BATCH = 200               # symbols per price request
SCREEN_MIN_BARS = 221            # a 200-day average that can be compared with a month ago
MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME = 20_000_000   # average traded per day over 50 days
RS_MIN_PERCENTILE = 70           # relative strength must beat 70% of liquid stocks
SCREEN_TOP_N = 40                # candidates shown to the AI each day
# Leveraged and inverse products are left out of the screen (the AI may still name one itself).
LEVERAGED_WORDS = ("2x", "3x", "-1x", "-2x", "-3x", "ultrapro", "ultrashort", "proshares ultra ", "leveraged",
                   "inverse", "daily target")

# --- Risk rules enforced by code, whatever the AI says --------------------------
MIN_TRADE_FRACTION = 0.01  # ignore changes smaller than 1% of the portfolio
DEFAULT_STOP = 0.08        # stop-loss 8% below the price when the AI gives none or an unusable one
MIN_STOP_GAP = 0.005       # a stop must sit at least 0.5% below the current price
MAX_STOP_GAP = 0.50        # and at most 50% below it
# No loss limit on the whole test and no cap per stock (Lucas's choice, 2026-11-13).

# --- Claude API ---------------------------------------------------------------
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL") or "claude-opus-5-5"     # research and decisions
CLAUDE_EFFORT = os.environ.get("CLAUDE_EFFORT") or "high"
RESEARCH_EFFORT = os.environ.get("RESEARCH_EFFORT") or "medium"
ANALYST_MODEL = os.environ.get("ANALYST_MODEL") or "claude-haiku-5-5"  # reads every candidate's news
ANALYST_EFFORT = os.environ.get("ANALYST_EFFORT") or "low"
MAX_OUTPUT_TOKENS = 16000
RESEARCH_MAX_TOKENS = 8000
ANALYST_MAX_TOKENS = 3000
ANALYST_NEWS = 15            # headlines read per candidate
ANALYST_REFRESH_DAYS = 7     # a candidate's card is rewritten after this many calendar days
ENSEMBLE_SIZE = 3            # independent decisions, combined by median weight
ENSEMBLE_MIN_VALID = 2       # fewer usable answers than this: hold
NEWS_SEARCH_MAX_USES = 5     # web searches in the market briefing on the first decision
ROUTINE_SEARCH_MAX_USES = 3  # on the other days: search results are most of the research bill
WEB_SEARCH_USD = 0.01        # $10 per 1,000 searches
RESEARCH_MAX_CONTINUATIONS = 2
# Upper bound used before spending: research can read many pages, so it is priced generously.
RESEARCH_WORST_CASE_USD = 1.50

# --- Free economic data (FRED public CSV downloads) -------------------------------
FRED_SERIES = {
    "DGS10": "US 10-year Treasury yield (%)",
    "DGS2": "US 2-year Treasury yield (%)",
    "T10Y2Y": "10-year minus 2-year yield spread (points)",
    "FEDFUNDS": "Fed funds rate (%)",
    "CPIAUCSL": "US consumer price index",
    "UNRATE": "US unemployment rate (%)",
    "BAMLH0A0HYM2": "High-yield bond spread (%)",
    "VIXCLS": "VIX volatility index",
}
# (input, output) US dollars per million tokens, first-party API prices.
MODEL_PRICES_PER_MTOK = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
HARD_API_LIMIT_USD = 200.0                     # Lucas's total API credit
API_BUDGET_USD = _float("API_BUDGET_USD", 60.0)  # what this test may spend (Lucas, 2026-11-13)

# --- Profitability accounting -------------------------------------------------
# Results are restated as if REAL_CAPITAL_USD had been invested, whatever the
# size of the paper account, with the fees a small European investor would pay.
REAL_CAPITAL_USD = _float("REAL_CAPITAL_USD", 1000.0)
FEE_PER_ORDER_USD = _float("FEE_PER_ORDER_USD", 1.00)  # broker fee per order
FEE_RATE = _float("FEE_RATE", 0.001)                  # spread + currency exchange
OTHER_MONTHLY_COSTS_USD = _float("OTHER_MONTHLY_COSTS_USD", 0.0)  # hosting etc.

# --- Prompt size ----------------------------------------------------------------
PROMPT_CLOSES = 20            # daily closes shown per candidate
RECENT_DECISIONS = 10         # past decisions shown to the AI

# --- Files ----------------------------------------------------------------------
STATE_PATH = os.environ.get("STATE_PATH") or "state/ledger.json"
REPORT_PATH = os.environ.get("REPORT_PATH") or "report.md"
DASHBOARD_DATA_PATH = os.environ.get("DASHBOARD_DATA_PATH") or "docs/data/dashboard.json"
LIVE_DATA_PATH = os.environ.get("LIVE_DATA_PATH") or "docs/data/live.json"

# --- Dashboard ------------------------------------------------------------------
HEADLINES_LIMIT = 40          # free headlines shown between decisions
MIN_DAYS_FOR_RATIOS = 5       # daily returns needed before Sharpe / Sortino / beta are shown
MIN_DAYS_FOR_ANNUAL = 20      # daily returns needed before an annualised return is shown
RISK_FREE_SERIES = "DTB3"     # 3-month US Treasury bill rate (FRED), used for Sharpe and Sortino


def check() -> None:
    """Refuse to start with settings that would break the budget or the accounting."""
    for model in (CLAUDE_MODEL, ANALYST_MODEL):
        if model not in MODEL_PRICES_PER_MTOK:
            raise SystemExit(f"No price known for model {model}; add it to MODEL_PRICES_PER_MTOK.")
    if not 0 < API_BUDGET_USD <= HARD_API_LIMIT_USD:
        raise SystemExit(f"API_BUDGET_USD must be above 0 and at most {HARD_API_LIMIT_USD}.")
    if REAL_CAPITAL_USD <= 0:
        raise SystemExit("REAL_CAPITAL_USD must be above 0.")
