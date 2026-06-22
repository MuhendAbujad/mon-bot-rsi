"""
RSI data layer — strictly uses Playwright to scrape the LuxAlgo Ultimate RSI
value directly from the user's TradingView chart URL, and captures a screenshot.

No fallback to yfinance or tradingview-ta. If the scraper returns None,
the monitor reports failure and skips the alert cycle.
"""

import datetime
import logging
from chart_scraper import get_ultimate_rsi
from config import CHART_URL

logger = logging.getLogger(__name__)


async def get_analysis() -> dict | None:
    """
    Scrapes the LuxAlgo Ultimate RSI and a chart screenshot from the TradingView URL.

    Returns a dict with:
      - 'rsi'              : float — the Ultimate RSI value read from the chart
      - 'screenshot_bytes' : bytes | None — PNG screenshot of the chart
      - 'source'           : 'playwright_luxalgo'
      - 'fetched_at'       : UTC timestamp string

    Returns None if the scraper could not extract the RSI value.
    """
    logger.info(f"[Monitor] Scraping Ultimate RSI from chart: {CHART_URL}")
    rsi, screenshot_bytes = await get_ultimate_rsi(CHART_URL)

    if rsi is None:
        logger.error("[Monitor] Playwright scraper returned None — skipping this cycle.")
        return None

    result = {
        "rsi":              rsi,
        "screenshot_bytes": screenshot_bytes,
        "source":           "playwright_luxalgo",
        "fetched_at":       datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    logger.info(f"[Monitor] LuxAlgo Ultimate RSI = {rsi:.3f}")
    return result


def get_rsi() -> float | None:
    """Sync convenience wrapper for test scripts."""
    import asyncio
    rsi, _ = asyncio.run(get_ultimate_rsi(CHART_URL))
    return rsi
