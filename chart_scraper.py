"""
Playwright-based scraper that opens the user's TradingView chart authenticated
with their sessionid cookie, then extracts the LuxAlgo Ultimate RSI value from
the indicator legend in the DOM.

Authentication is required because LuxAlgo Ultimate RSI is a protected Pine
Script indicator — it only computes for the chart owner when logged in.

Resilience: get_ultimate_rsi() retries the full scrape up to MAX_RETRIES times
on any transient error (network blip, Playwright crash, TradingView timeout).
Each individual Playwright operation is also wrapped in its own try/except so
a single micro-failure (consent banner click, JS eval, screenshot) never kills
the whole cycle.
"""

import asyncio
import os
import re
import logging
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeout,
    Error as PlaywrightError,
)

logger = logging.getLogger(__name__)

_CHROMIUM_PATH = "/nix/store/qa9cnw4v5xkxyip6mb9kxqfq1z4x2dx1-chromium-138.0.7204.100/bin/chromium-browser"

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-setuid-sandbox",
    "--single-process",
    "--disable-software-rasterizer",
]

_CONSENT_SELECTORS = [
    '[data-name="cookie-policy-accept"]',
    'button[id*="accept"]',
    'button:text("Accept all")',
    'button:text("Accepter tout")',
    'button:text("Accept")',
    '.acceptAll',
]

# Waits until any legend text node contains a decimal number next to Ultimate RSI
_JS_WAIT_FOR_VALUE = """
() => {
    // Match numbers with either dot or comma as decimal separator (e.g. "84,490" or "84.490")
    const numPat = /\\b(\\d{1,3}[.,]\\d{1,4})\\b/;
    const items = document.querySelectorAll('[data-qa-id="legend-source-item"]');
    for (const item of items) {
        const txt = item.innerText || "";
        if ((txt.includes("Ultimate RSI") || (txt.includes("LuxAlgo") && txt.includes("RSI")))
            && numPat.test(txt)) {
            return true;
        }
    }
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    let node;
    while ((node = walker.nextNode())) {
        const t = node.textContent || "";
        if ((t.includes("Ultimate RSI") || (t.includes("LuxAlgo") && t.includes("RSI")))
            && numPat.test(t)) {
            return true;
        }
    }
    return false;
}
"""

_JS_EXTRACT = """
() => {
    // TradingView uses locale-formatted numbers: "84,490" (French) or "84.490" (English)
    // We split the legend text on newlines and look for lines that are pure numbers.
    // The RSI value line appears right after the fixed threshold lines (80, 20, 50).

    function parseLocaleNum(s) {
        // "84,490" -> 84.490  |  "84.490" -> 84.490
        return parseFloat(s.trim().replace(',', '.'));
    }

    function isRSICandidate(v) {
        return !isNaN(v) && v >= 0 && v <= 100;
    }

    function extractFromText(txt) {
        // Legend format: "LuxAlgo - Ultimate RSI\\n14\\nRMA\\nclose\\n14\\nEMA\\n80\\n20\\n<RSI>\\n80,000\\n50,000\\n20,000"
        // Strategy: find the position of "20" (oversold threshold) in the lines array,
        // then take the very next decimal line — that is the live RSI value.
        const lines = txt.split(/[\\n\\r]+/).map(l => l.trim()).filter(l => l.length > 0);

        // Find the last occurrence of "20" (the oversold param) among integer lines
        let afterIdx = -1;
        for (let i = 0; i < lines.length; i++) {
            if (lines[i] === "20") afterIdx = i;
        }
        if (afterIdx >= 0 && afterIdx + 1 < lines.length) {
            const candidate = lines[afterIdx + 1];
            if (/^\\d{1,3}[,.]\\d{1,6}$/.test(candidate)) {
                const v = parseLocaleNum(candidate);
                if (isRSICandidate(v)) return [v];
            }
        }

        // Fallback: first decimal number that is NOT a known fixed threshold
        const fixed = new Set([80, 50, 20, 0, 100]);
        for (const line of lines) {
            if (/^\\d{1,3}[,.]\\d{1,6}$/.test(line)) {
                const v = parseLocaleNum(line);
                if (isRSICandidate(v) && !fixed.has(v)) {
                    return [v];
                }
            }
        }
        return [];
    }

    // Strategy 1: legend items with data-qa-id
    const items = document.querySelectorAll('[data-qa-id="legend-source-item"]');
    for (const item of items) {
        const txt = item.innerText || item.textContent || "";
        if (txt.includes("Ultimate RSI") || (txt.includes("LuxAlgo") && txt.includes("RSI"))) {
            const candidates = extractFromText(txt);
            if (candidates.length) return { value: candidates[0], source: "legend-item-lines" };
        }
    }

    // Strategy 2: data-test-id-value-title attributes
    const testItems = document.querySelectorAll('[data-test-id-value-title]');
    for (const el of testItems) {
        const title = el.getAttribute('data-test-id-value-title') || "";
        if (title.includes("Ultimate") || title.includes("RSI")) {
            const txt = el.innerText || el.textContent || "";
            const candidates = extractFromText(txt);
            if (candidates.length) return { value: candidates[0], source: "data-test-id" };
        }
    }

    // Strategy 3: walk text nodes, grab the full parent element text
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    let node;
    while ((node = walker.nextNode())) {
        const text = node.textContent || "";
        if (text.includes("Ultimate RSI") || (text.includes("LuxAlgo") && text.includes("RSI"))) {
            const parent = node.parentElement;
            const pText = parent ? (parent.innerText || parent.textContent || "") : text;
            const candidates = extractFromText(pText);
            if (candidates.length) return { value: candidates[0], source: "text-node-parent" };
        }
    }

    return null;
}
"""

_JS_DEBUG_LEGEND = """
() => {
    const results = [];
    const items = document.querySelectorAll('[data-qa-id="legend-source-item"]');
    for (const item of items) {
        results.push(item.innerText || item.textContent || "");
    }
    return results;
}
"""


_MAX_RETRIES = 3          # total attempts on transient failures
_RETRY_DELAY_S = 5        # seconds to wait between retries


async def _scrape_once(
    chart_url: str,
    session_id: str,
    timeout_s: int,
) -> tuple[float | None, bytes | None]:
    """
    Single scrape attempt. Raises on fatal Playwright errors so the caller
    can decide whether to retry. All non-fatal sub-operations are wrapped in
    their own try/except and degrade gracefully.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            executable_path=_CHROMIUM_PATH,
            args=_LAUNCH_ARGS,
        )
        try:
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="UTC",
            )

            # Inject TradingView auth cookies BEFORE navigating
            await context.add_cookies([
                {
                    "name": "sessionid",
                    "value": session_id,
                    "domain": ".tradingview.com",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                },
                {
                    "name": "sessionid_sign",
                    "value": session_id,
                    "domain": ".tradingview.com",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                },
            ])

            page = await context.new_page()

            # Block video/audio only — keep images & fonts for chart rendering
            try:
                await page.route(
                    "**/*.{mp4,webm,ogg,ogv}",
                    lambda route: route.abort(),
                )
            except Exception as e:
                logger.debug(f"[Scraper] Route setup warning: {e}")

            # ── Navigate ────────────────────────────────────────────────────
            logger.info(f"[Scraper] Navigating to {chart_url}")
            try:
                await page.goto(
                    chart_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_s * 1000,
                )
            except PlaywrightTimeout:
                logger.warning("[Scraper] Page load timed out — proceeding with partial load")
            except PlaywrightError as e:
                # Network micro-coupure or connection reset — re-raise so retry kicks in
                raise RuntimeError(f"Navigation failed: {e}") from e

            # ── Dismiss cookie banners ───────────────────────────────────────
            for sel in _CONSENT_SELECTORS:
                try:
                    await page.click(sel, timeout=2000)
                    logger.debug(f"[Scraper] Dismissed consent via: {sel}")
                    break
                except (PlaywrightTimeout, PlaywrightError):
                    pass
                except Exception as e:
                    logger.debug(f"[Scraper] Consent click error ({sel}): {e}")

            logger.info(f"[Scraper] URL after load: {page.url}")

            # ── Wait for RSI value in legend ─────────────────────────────────
            logger.info("[Scraper] Waiting for Ultimate RSI value in legend…")
            try:
                await page.wait_for_function(
                    _JS_WAIT_FOR_VALUE,
                    timeout=60000,
                    polling=1500,
                )
                logger.info("[Scraper] RSI value detected in DOM")
            except PlaywrightTimeout:
                logger.warning("[Scraper] RSI value did not appear within 60s — trying extraction anyway")
            except PlaywrightError as e:
                logger.warning(f"[Scraper] wait_for_function error: {e} — trying extraction anyway")

            # Let the chart canvas fully render
            await asyncio.sleep(3)

            # ── Debug: log visible legends ───────────────────────────────────
            try:
                legends = await page.evaluate(_JS_DEBUG_LEGEND)
                for i, leg in enumerate(legends):
                    logger.info(f"[Scraper] Legend[{i}]: {leg[:120]!r}")
            except (PlaywrightError, Exception) as e:
                logger.debug(f"[Scraper] Debug legend failed: {e}")

            # ── Extract RSI — up to 5 JS evaluation attempts ─────────────────
            rsi: float | None = None
            for attempt in range(1, 6):
                try:
                    result = await page.evaluate(_JS_EXTRACT)
                except (PlaywrightError, Exception) as e:
                    logger.warning(f"[Scraper] JS evaluate error (attempt {attempt}): {e}")
                    result = None

                if result is not None:
                    rsi = result["value"]
                    src = result["source"]
                    logger.info(f"[Scraper] Ultimate RSI = {rsi:.3f} (attempt {attempt}, source={src})")
                    break
                if attempt < 5:
                    wait = attempt * 2
                    logger.debug(f"[Scraper] Attempt {attempt} found nothing — waiting {wait}s")
                    await asyncio.sleep(wait)

            # ── Hover fallback ───────────────────────────────────────────────
            if rsi is None:
                logger.info("[Scraper] Trying hover strategy…")
                try:
                    await page.mouse.move(960, 850)
                    await asyncio.sleep(2)
                    result = await page.evaluate(_JS_EXTRACT)
                    if result is not None:
                        rsi = result["value"]
                        logger.info(f"[Scraper] Ultimate RSI = {rsi:.3f} (hover strategy)")
                except (PlaywrightError, Exception) as e:
                    logger.debug(f"[Scraper] Hover strategy failed: {e}")

            if rsi is None:
                logger.error(
                    "[Scraper] Could not extract Ultimate RSI — check that sessionid "
                    "is valid and the LuxAlgo indicator is visible on the chart."
                )

            # ── Screenshot ───────────────────────────────────────────────────
            screenshot_bytes: bytes | None = None
            try:
                screenshot_bytes = await page.screenshot(full_page=False)
                logger.info(f"[Scraper] Screenshot captured ({len(screenshot_bytes):,} bytes)")
            except (PlaywrightError, Exception) as e:
                logger.warning(f"[Scraper] Screenshot failed: {e}")

            return rsi, screenshot_bytes

        finally:
            # Always close the browser, even if something above raised
            try:
                await browser.close()
            except Exception:
                pass


async def get_ultimate_rsi(
    chart_url: str,
    timeout_s: int = 90,
) -> tuple[float | None, bytes | None]:
    """
    Resilient entry point: attempts _scrape_once() up to _MAX_RETRIES times.

    A transient error (network blip, Playwright crash, TradingView hiccup)
    triggers a short delay then a full retry with a fresh browser instance.
    Returns (None, None) only after all retries are exhausted.
    """
    session_id = os.environ.get("TRADINGVIEW_SESSION_ID", "").strip()
    if not session_id:
        logger.error("[Scraper] TRADINGVIEW_SESSION_ID is not set — cannot authenticate.")
        return None, None

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            rsi, screenshot = await _scrape_once(chart_url, session_id, timeout_s)
            return rsi, screenshot
        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                logger.warning(
                    f"[Scraper] Attempt {attempt}/{_MAX_RETRIES} failed "
                    f"({type(exc).__name__}: {exc}) — retrying in {_RETRY_DELAY_S}s…"
                )
                await asyncio.sleep(_RETRY_DELAY_S)
            else:
                logger.error(
                    f"[Scraper] All {_MAX_RETRIES} attempts failed. "
                    f"Last error: {type(exc).__name__}: {exc}",
                    exc_info=True,
                )

    return None, None
