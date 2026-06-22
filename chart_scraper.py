"""
Playwright-based scraper adapted for Docker/Render environment.
Authentication via sessionid cookie to extract LuxAlgo Ultimate RSI.
"""

import asyncio
import os
import logging
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeout,
    Error as PlaywrightError,
)

logger = logging.getLogger(__name__)

# Arguments optimisés pour Docker/headless
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

_JS_WAIT_FOR_VALUE = """
() => {
    const numPat = /\\b(\\d{1,3}[.,]\\d{1,4})\\b/;
    const items = document.querySelectorAll('[data-qa-id="legend-source-item"]');
    for (const item of items) {
        const txt = item.innerText || "";
        if ((txt.includes("Ultimate RSI") || (txt.includes("LuxAlgo") && txt.includes("RSI")))
            && numPat.test(txt)) {
            return true;
        }
    }
    return false;
}
"""

_JS_EXTRACT = """
() => {
    function parseLocaleNum(s) { return parseFloat(s.trim().replace(',', '.')); }
    function isRSICandidate(v) { return !isNaN(v) && v >= 0 && v <= 100; }
    
    function extractFromText(txt) {
        const lines = txt.split(/[\\n\\r]+/).map(l => l.trim()).filter(l => l.length > 0);
        let afterIdx = -1;
        for (let i = 0; i < lines.length; i++) { if (lines[i] === "20") afterIdx = i; }
        if (afterIdx >= 0 && afterIdx + 1 < lines.length) {
            const candidate = lines[afterIdx + 1];
            if (/^\\d{1,3}[,.]\\d{1,6}$/.test(candidate)) {
                const v = parseLocaleNum(candidate);
                if (isRSICandidate(v)) return [v];
            }
        }
        return [];
    }

    const items = document.querySelectorAll('[data-qa-id="legend-source-item"]');
    for (const item of items) {
        const txt = item.innerText || "";
        if (txt.includes("Ultimate RSI") || (txt.includes("LuxAlgo") && txt.includes("RSI"))) {
            const candidates = extractFromText(txt);
            if (candidates.length) return { value: candidates[0], source: "legend-item-lines" };
        }
    }
    return null;
}
"""

_MAX_RETRIES = 3
_RETRY_DELAY_S = 5

async def _scrape_once(chart_url: str, session_id: str, timeout_s: int) -> tuple[float | None, bytes | None]:
    async with async_playwright() as p:
        # Lancement sans executable_path : Playwright utilisera Chromium intégré à l'image Docker
        browser = await p.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        try:
            context = await browser.new_context(viewport={"width": 1920, "height": 1080}, locale="en-US")
            await context.add_cookies([
                {"name": "sessionid", "value": session_id, "domain": ".tradingview.com", "path": "/", "httpOnly": True, "secure": True, "sameSite": "None"},
                {"name": "sessionid_sign", "value": session_id, "domain": ".tradingview.com", "path": "/", "httpOnly": True, "secure": True, "sameSite": "None"},
            ])
            page = await context.new_page()
            
            await page.goto(chart_url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            
            # Attente RSI
            try:
                await page.wait_for_function(_JS_WAIT_FOR_VALUE, timeout=60000)
            except:
                pass
                
            await asyncio.sleep(3)
            result = await page.evaluate(_JS_EXTRACT)
            rsi = result["value"] if result else None
            
            screenshot = await page.screenshot()
            return rsi, screenshot
        finally:
            await browser.close()

async def get_ultimate_rsi(chart_url: str, timeout_s: int = 90) -> tuple[float | None, bytes | None]:
    session_id = os.environ.get("TRADINGVIEW_SESSION_ID", "").strip()
    if not session_id:
        return None, None
        
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return await _scrape_once(chart_url, session_id, timeout_s)
        except Exception as e:
            if attempt == _MAX_RETRIES: raise
            await asyncio.sleep(_RETRY_DELAY_S)
    return None, None
