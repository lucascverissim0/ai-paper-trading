"""Every setting of the paper-trading test lives here.

Change numbers here (or through environment variables), never inside the logic.
"""
from __future__ import annotations

import os


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


# --- What the AI may hold (long only, no leverage, no shorting) -------------
UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD"]
UNIVERSE_DESCRIPTIONS = {
    "SPY": "US large companies (S&P 500)",
    "QQQ": "US large tech-heavy companies (Nasdaq-100)",
    "IWM": "US small companies (Russell 2000)",
    "EFA": "Developed markets outside North America",
    "EEM": "Emerging markets",
    "TLT": "US government bonds, 20+ years",
    "IEF": "US government bonds, 7-10 years",
    "GLD": "Gold",
}
BENCHMARK = "SPY"  # the AI must beat simply buying and holding this

# --- Risk rules enforced by code, whatever the AI says ----------------------
MAX_WEIGHT = 0.50          # at most 50% of the portfolio in one ETF
MIN_TRADE_FRACTION = 0.05  # ignore changes smaller than 5% of the portfolio (a $1 fee on $50 is already 2%)
MAX_DRAWDOWN = 0.15        # sell everything and stop if value falls 15% from its peak

# --- Claude API ---------------------------------------------------------------
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL") or "claude-opus-5-5"
CLAUDE_EFFORT = os.environ.get("CLAUDE_EFFORT") or "high"      # for decisions
RESEARCH_EFFORT = os.environ.get("RESEARCH_EFFORT") or "medium"
MAX_OUTPUT_TOKENS = 16000
RESEARCH_MAX_TOKENS = 8000
ENSEMBLE_SIZE = 3            # independent decisions, combined by median weight
ENSEMBLE_MIN_VALID = 2       # fewer usable answers than this: hold
NEWS_SEARCH_MAX_USES = 5     # web searches per research briefing
WEB_SEARCH_USD = 0.01        # $10 per 1,000 searches
RESEARCH_MAX_CONTINUATIONS = 2
# Upper bound used before spending: research can read many pages, so it is priced generously.
RESEARCH_WORST_CASE_USD = 1.50

# --- When Claude is woken up (no API cost on other days) ----------------------
MIN_REVIEW_DAYS = 1
MAX_REVIEW_DAYS = 10         # trading days; Claude picks within this range
JOLT_SPY_DAY = 0.02          # SPY moved more than 2% on the last completed day
JOLT_HELD_ETF = 0.05         # a held ETF moved more than 5% since the last decision
JOLT_PORTFOLIO = 0.05        # portfolio fell more than 5% since the last decision

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
API_BUDGET_USD = _float("API_BUDGET_USD", 20.0)  # what this test may spend

# --- Profitability accounting -------------------------------------------------
# Results are restated as if REAL_CAPITAL_USD had been invested, whatever the
# size of the paper account, with the fees a small European investor would pay.
REAL_CAPITAL_USD = _float("REAL_CAPITAL_USD", 1000.0)
FEE_PER_ORDER_USD = _float("FEE_PER_ORDER_USD", 1.00)  # broker fee per order
FEE_RATE = _float("FEE_RATE", 0.001)                  # spread + currency exchange
OTHER_MONTHLY_COSTS_USD = _float("OTHER_MONTHLY_COSTS_USD", 0.0)  # hosting etc.

# --- Prompt size ----------------------------------------------------------------
PROMPT_CLOSES = 40            # daily closes shown per ETF
HISTORY_CALENDAR_DAYS = 420   # history fetched to compute 12-month figures
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
    if CLAUDE_MODEL not in MODEL_PRICES_PER_MTOK:
        raise SystemExit(f"No price known for model {CLAUDE_MODEL}; add it to MODEL_PRICES_PER_MTOK.")
    if not 0 < API_BUDGET_USD <= HARD_API_LIMIT_USD:
        raise SystemExit(f"API_BUDGET_USD must be above 0 and at most {HARD_API_LIMIT_USD}.")
    if REAL_CAPITAL_USD <= 0:
        raise SystemExit("REAL_CAPITAL_USD must be above 0.")
