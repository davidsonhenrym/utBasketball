#!/usr/bin/env python3
"""
Transfer Portal Scraper — ON3 + 247Sports (no API key required)
================================================================
Scrapes the public-facing transfer portal pages using Playwright
(headless Chrome), which handles the JavaScript rendering both
sites rely on.

SETUP (run once):
  pip install playwright beautifulsoup4 openpyxl
  playwright install chromium

USAGE:
  python portal_scraper.py

OUTPUT:
  • Prints a table of all portal entrants found
  • Highlights any players on your WATCHLIST
  • Saves portal_snapshot_YYYY-MM-DD.json for history
"""

import asyncio
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Auto-install dependencies ──────────────────────────────────────────────────
def _install(pkg):
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", pkg],
                          stdout=subprocess.DEVNULL)

for _pkg in ("playwright", "beautifulsoup4", "openpyxl"):
    try:
        __import__(_pkg.replace("-", "_"))
    except ImportError:
        print(f"Installing {_pkg}...")
        _install(_pkg)

from bs4 import BeautifulSoup

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    PW_AVAILABLE = True
except ImportError:
    PW_AVAILABLE = False
    print("Playwright install failed — run: pip install playwright && playwright install chromium")
    sys.exit(1)

# =============================================================================
# CONFIGURE — add any player names you want to flag
# =============================================================================

WATCHLIST = [
    # Paste names here — partial matches work (case-insensitive)
    # Examples from your existing tracker list:
    "Dior Johnson",
    "Trevian Carson",
    "Cruz Davis",
    "Trevan Leonhardt",
    "Gavin Doty",
    "Jack Karasinski",
    "Tyler Hendricks",
    "Lewis Walker",
    "Isaiah Malone",
    "TJ Nadeau",
    "MJ Thomas",
    "Francis Folefac",
    "Dayan Nessah",
    "Christian Bliss",
    "Andrew Holifield",
    "Dennis Parker Jr.",
    "Blake Barkley",
    # Add your portal targets here:
]

SPORT_FILTER = "basketball"   # used in ON3 URL
DIVISION     = "i"            # D1

# =============================================================================

SCRIPT_DIR = Path(__file__).parent

ON3_URL = (
    f"https://www.on3.com/transfer-portal/industry/{SPORT_FILTER}/"
)
# 247 uses a season-based URL; update year as needed
SPORTS247_URL = "https://247sports.com/Season/2026-Basketball/TransferPortal/"


# ── Playwright helpers ─────────────────────────────────────────────────────────

async def _make_browser(pw):
    """Launch a headless Chromium that looks like a normal browser."""
    return await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
        ],
    )


async def _open_page(browser, url, wait_selector, timeout=30_000):
    """Open a page and wait until a key element appears."""
    ctx  = await browser.new_context(
        viewport={"width": 1440, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-US",
    )
    page = await ctx.new_page()
    # Suppress image/font downloads to speed things up
    await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf}", lambda r: r.abort())
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        await page.wait_for_selector(wait_selector, timeout=timeout)
    except PWTimeout:
        print(f"  Timeout waiting for '{wait_selector}' on {url}")
    return page


# ── ON3 scraper ────────────────────────────────────────────────────────────────

async def scrape_on3(browser) -> list[dict]:
    """
    ON3 renders its portal table client-side.  We scroll to load all rows
    (they paginate via infinite scroll) then parse the DOM.
    """
    print("  [ON3] Opening transfer portal page...")
    players = []

    try:
        page = await _open_page(
            browser,
            ON3_URL,
            # The player cards live inside a list; this selector may need
            # updating if ON3 redesigns — check DevTools if it breaks.
            "div[class*='PlayerCard'], a[href*='/player/'], div[class*='portal']",
            timeout=25_000,
        )
    except Exception as exc:
        print(f"  [ON3] Page load failed: {exc}")
        return players

    # Scroll to trigger lazy-loaded rows (up to ~500 players)
    print("  [ON3] Scrolling to load all entries...")
    prev_height = 0
    for _ in range(30):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.2)
        cur_height = await page.evaluate("document.body.scrollHeight")
        if cur_height == prev_height:
            break
        prev_height = cur_height

    html = await page.content()
    await page.context.close()

    soup = BeautifulSoup(html, "html.parser")

    # ON3 wraps each portal entry in a card/row — try several selector patterns
    cards = (
        soup.select("div[class*='PlayerCard']")
        or soup.select("div[class*='portalPlayer']")
        or soup.select("article[class*='player']")
    )

    if not cards:
        # Fallback: look for player profile links
        seen = set()
        for a in soup.select("a[href*='/player/']"):
            name = a.get_text(strip=True)
            href = a.get("href", "")
            if name and len(name) > 3 and href not in seen:
                seen.add(href)
                players.append({
                    "source": "ON3",
                    "name":   name,
                    "from":   "",
                    "to":     "",
                    "pos":    "",
                    "stars":  "",
                    "nil":    "",
                    "date":   "",
                    "url":    f"https://www.on3.com{href}" if href.startswith("/") else href,
                })
        print(f"  [ON3] Fallback link parse: {len(players)} player links found")
        return players

    for card in cards:
        name  = _text(card, "[class*='name'], [class*='Name'], h3, h4")
        from_ = _text(card, "[class*='school'], [class*='School'], [class*='from']")
        to_   = _text(card, "[class*='destination'], [class*='commit'], [class*='to']")
        pos   = _text(card, "[class*='position'], [class*='Position'], [class*='pos']")
        stars = _text(card, "[class*='star'], [class*='Star'], [class*='rating']")
        nil   = _text(card, "[class*='nil'], [class*='NIL'], [class*='value']")
        date  = _text(card, "[class*='date'], [class*='Date'], time")
        link  = card.select_one("a[href*='/player/']")
        url   = ""
        if link:
            url = link.get("href", "")
            if url.startswith("/"):
                url = f"https://www.on3.com{url}"

        if name:
            players.append({
                "source": "ON3",
                "name":   name,
                "from":   from_,
                "to":     to_,
                "pos":    pos,
                "stars":  stars,
                "nil":    nil,
                "date":   date,
                "url":    url,
            })

    print(f"  [ON3] {len(players)} portal entries found")
    return players


# ── 247Sports scraper ──────────────────────────────────────────────────────────

async def scrape_247(browser) -> list[dict]:
    """
    247Sports portal page — mix of SSR and client-side.
    We let Playwright render it fully, then parse the table rows.
    """
    print("  [247] Opening transfer portal page...")
    players = []

    try:
        page = await _open_page(
            browser,
            SPORTS247_URL,
            "ul.transfer-portal, div[class*='portal'], .transfer-list, table",
            timeout=25_000,
        )
    except Exception as exc:
        print(f"  [247] Page load failed: {exc}")
        return players

    # Dismiss any pop-ups / cookie banners
    for sel in ["button[id*='onetrust-accept']", "button[class*='close']",
                "[aria-label*='Close']", "#onetrust-accept-btn-handler"]:
        try:
            await page.click(sel, timeout=2000)
        except Exception:
            pass

    # 247 may paginate — click "Load More" up to 20 times
    print("  [247] Loading all portal entries...")
    for _ in range(20):
        try:
            btn = await page.wait_for_selector(
                "a[class*='load-more'], button[class*='load-more'], "
                "a:text('Load More'), button:text('Load More')",
                timeout=3000,
            )
            await btn.click()
            await asyncio.sleep(1.5)
        except Exception:
            break

    html = await page.content()
    await page.context.close()

    soup = BeautifulSoup(html, "html.parser")

    # 247 uses a list or table structure for portal players
    rows = (
        soup.select("ul.transfer-portal > li")
        or soup.select("li[class*='portal-player']")
        or soup.select("li[class*='transfer']")
        or soup.select("tr[class*='player']")
    )

    if not rows:
        # Fallback: anchor tags pointing to player profiles
        seen = set()
        for a in soup.select("a[href*='/player/']"):
            name = a.get_text(strip=True)
            href = a.get("href", "")
            if name and len(name) > 3 and href not in seen:
                seen.add(href)
                players.append({
                    "source": "247",
                    "name":   name,
                    "from":   "",
                    "to":     "",
                    "pos":    "",
                    "stars":  "",
                    "nil":    "",
                    "date":   "",
                    "url":    href,
                })
        print(f"  [247] Fallback link parse: {len(players)} player links found")
        return players

    for row in rows:
        name  = _text(row, ".name, [class*='name'], h2, h3, strong")
        from_ = _text(row, ".school, [class*='school'], [class*='from']")
        to_   = _text(row, ".destination, [class*='destination'], [class*='commit']")
        pos   = _text(row, ".position, [class*='position']")
        stars = _text(row, ".ranking, [class*='star'], [class*='rating']")
        date  = _text(row, ".date, time, [class*='date']")
        link  = row.select_one("a[href*='/player/']")
        url   = link.get("href", "") if link else ""

        if name:
            players.append({
                "source": "247",
                "name":   name,
                "from":   from_,
                "to":     to_,
                "pos":    pos,
                "stars":  stars,
                "nil":    "",
                "date":   date,
                "url":    url,
            })

    print(f"  [247] {len(players)} portal entries found")
    return players


# ── Utility ────────────────────────────────────────────────────────────────────

def _text(tag, selector: str) -> str:
    """Try multiple comma-separated selectors; return first match's text."""
    for sel in [s.strip() for s in selector.split(",")]:
        el = tag.select_one(sel)
        if el:
            return el.get_text(strip=True)
    return ""


def _on_watchlist(name: str) -> bool:
    n = name.lower()
    return any(w.lower() in n or n in w.lower() for w in WATCHLIST)


def deduplicate(players: list[dict]) -> list[dict]:
    """Keep one entry per player name (prefer ON3 if both sources)."""
    seen = {}
    for p in players:
        key = _normalize_name(p["name"])
        if key not in seen or p["source"] == "ON3":
            seen[key] = p
    return list(seen.values())


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


# ── Output ─────────────────────────────────────────────────────────────────────

def print_portal_report(players: list[dict]):
    today = datetime.now().strftime("%B %d, %Y")
    W = 110
    print(f"\n{'='*W}")
    print(f"  TRANSFER PORTAL REPORT  |  {today}  |  {len(players)} players")
    print(f"{'='*W}")

    watchlist_hits = [p for p in players if _on_watchlist(p["name"])]
    if watchlist_hits:
        print(f"\n  *** {len(watchlist_hits)} WATCHLIST PLAYER(S) IN PORTAL ***\n")
        for p in sorted(watchlist_hits, key=lambda x: x["name"]):
            dest = f"→ {p['to']}" if p["to"] else ""
            print(f"  !! {p['name']:<28} {p['from']:<24} {dest:<22} [{p['source']}]")
        print()

    print(f"\n  {'Name':<30}{'From':<26}{'To':<22}{'Pos':>5}{'Stars':>7}  Src")
    print(f"  {'-'*W}")

    for p in sorted(players, key=lambda x: x["name"]):
        flag  = "**" if _on_watchlist(p["name"]) else "  "
        print(
            f"{flag} {p['name']:<30}{p['from']:<26}{p['to']:<22}"
            f"{p['pos']:>5}{p['stars']:>7}  {p['source']}"
        )

    print(f"\n  ** = on your watchlist")
    print(f"{'='*W}\n")


def save_snapshot(players: list[dict]):
    today = datetime.now().strftime("%Y-%m-%d")
    path  = SCRIPT_DIR / f"portal_snapshot_{today}.json"
    data  = {
        "generated_at": datetime.now().isoformat(),
        "total":        len(players),
        "watchlist_in_portal": [p["name"] for p in players if _on_watchlist(p["name"])],
        "players": players,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"Snapshot saved: {path}")

    # Also write a fixed-name file so the dashboard can auto-sync without a file picker
    latest = SCRIPT_DIR / "portal_snapshot_latest.json"
    with open(latest, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"Latest snapshot updated: {latest}")

    return path


# ── Main ───────────────────────────────────────────────────────────────────────

async def _run():
    print("Transfer Portal Scraper — ON3 + 247Sports")
    print(f"Watchlist: {len(WATCHLIST)} players\n")

    async with async_playwright() as pw:
        browser = await _make_browser(pw)
        try:
            on3_players, s247_players = await asyncio.gather(
                scrape_on3(browser),
                scrape_247(browser),
            )
        finally:
            await browser.close()

    all_players = deduplicate(on3_players + s247_players)
    print(f"\nCombined (deduped): {len(all_players)} portal entries\n")

    print_portal_report(all_players)
    save_snapshot(all_players)

    return all_players


def main():
    asyncio.run(_run())


if __name__ == "__main__":
    main()
