"""Free economic data from FRED, as plain text for the decision prompt. A failing series is skipped."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable, Optional
from urllib.request import Request, urlopen

from . import config

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}"


def fetch_text(url: str) -> str:
    # Python's default client name: FRED silently drops requests with an unknown custom one.
    req = Request(url)
    with urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_fred_csv(text: str) -> list[tuple[str, float]]:
    rows = []
    for line in text.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) != 2:
            continue
        try:
            rows.append((parts[0], float(parts[1])))
        except ValueError:  # "." marks a missing day
            continue
    return rows


def _value_on_or_before(rows: list[tuple[str, float]], day: str) -> Optional[float]:
    before = [v for d, v in rows if d <= day]
    return before[-1] if before else None


def describe(series: str, label: str, rows: list[tuple[str, float]], today: str) -> Optional[str]:
    if not rows:
        return None
    last_date, last = rows[-1]
    if series == "CPIAUCSL":
        year_ago = _value_on_or_before(rows, (date.fromisoformat(last_date) - timedelta(days=365)).isoformat())
        if not year_ago:
            return None
        return f"- US inflation (CPI, 12 months to {last_date[:7]}): {(last / year_ago - 1) * 100:.1f}%"
    t = date.fromisoformat(today)
    m1 = _value_on_or_before(rows, (t - timedelta(days=30)).isoformat())
    m3 = _value_on_or_before(rows, (t - timedelta(days=91)).isoformat())
    changes = ", ".join(
        f"{name} {last - old:+.2f}" for name, old in (("1 month", m1), ("3 months", m3)) if old is not None
    )
    return f"- {label}: {last:.2f} on {last_date}" + (f" (change: {changes})" if changes else "")


def economy_snapshot(today: str, fetch: Callable[[str], str] = fetch_text) -> str:
    start = (date.fromisoformat(today) - timedelta(days=430)).isoformat()
    lines = []
    for series, label in config.FRED_SERIES.items():
        try:
            rows = parse_fred_csv(fetch(FRED_URL.format(series=series, start=start)))
            line = describe(series, label, rows, today)
        except Exception:  # network or format problem: leave this series out
            line = None
        if line:
            lines.append(line)
    return "\n".join(lines)


def latest_value(series: str, today: str, fetch: Callable[[str], str] = fetch_text) -> Optional[float]:
    """Most recent value of one FRED series, or None if it cannot be read."""
    start = (date.fromisoformat(today) - timedelta(days=30)).isoformat()
    try:
        rows = parse_fred_csv(fetch(FRED_URL.format(series=series, start=start)))
    except Exception:
        return None
    return rows[-1][1] if rows else None
