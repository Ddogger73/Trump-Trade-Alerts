"""Watches Quiver's Nancy Pelosi page and posts new trades to Discord.

Same approach as monitor.py (the Trump tracker): the trades table is loaded by
JavaScript, so a headless browser (Playwright) renders the page first.

State is kept in seen_pelosi.json. The first run records the existing trades
without alerting, then only new trades trigger a message.
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "https://www.quiverquant.com/congresstrading/politician/NancyPelosi-P000197"
SEEN_FILE = Path("seen_pelosi.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# This page has several tables (holdings, lobbyists, legislation...), so find
# the trades table by its headers and wait until it has real rows.
TABLE_READY_JS = """
() => {
  for (const t of document.querySelectorAll('table')) {
    const head = (t.querySelector('thead') || t).innerText.toLowerCase();
    if (!head.includes('transaction') || !head.includes('traded')) continue;
    const rows = [...t.querySelectorAll('tbody tr')];
    if (rows.length && rows[0].querySelectorAll('td').length >= 4) return true;
  }
  return false;
}
"""


def clean(text: str) -> str:
    text = re.sub(r"[ \t\u00a0]+", " ", text.strip())
    return re.sub(r"\s*\n\s*", " | ", text)


def scrape() -> list[dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA, viewport={"width": 1400, "height": 1000})
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_function(TABLE_READY_JS, timeout=60_000)
            page.wait_for_timeout(2_000)
        except Exception:
            page.screenshot(path="debug_pelosi.png", full_page=True)
            Path("debug_pelosi.html").write_text(page.content(), encoding="utf-8")
            raise

        table = (
            page.locator("table")
            .filter(has=page.locator("th", has_text="Transaction"))
            .filter(has=page.locator("th", has_text="Traded"))
            .first
        )
        headers = [clean(h).lower() for h in table.locator("thead th").all_inner_texts()]

        def col(name: str, default: int) -> int:
            for i, h in enumerate(headers):
                if name in h:
                    return i
            return default

        i_stock, i_tx = col("stock", 0), col("transaction", 1)
        i_filed, i_traded = col("filed", 2), col("traded", 3)
        i_desc = col("description", 4)

        trades = []
        rows = table.locator("tbody tr")
        for r in range(rows.count()):
            cells = [clean(c) for c in rows.nth(r).locator("td").all_inner_texts()]
            if len(cells) <= max(i_stock, i_tx, i_filed, i_traded):
                continue
            stock, tx = cells[i_stock], cells[i_tx]
            filed, traded = cells[i_filed], cells[i_traded]
            desc = cells[i_desc] if i_desc < len(cells) else ""
            # Excess return changes over time, so it is NOT part of the ID.
            key = "|".join([stock, tx, filed, traded, desc])
            trades.append(
                {
                    "id": hashlib.sha1(key.encode()).hexdigest(),
                    "stock": stock,
                    "transaction": tx,
                    "filed": filed,
                    "traded": traded,
                    "description": desc,
                }
            )
        browser.close()
        return trades


def post_discord(content: str) -> None:
    if not WEBHOOK:
        print("DISCORD_WEBHOOK_URL not set; would have sent:\n" + content)
        return
    req = urllib.request.Request(
        WEBHOOK,
        data=json.dumps({"content": content}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "trade-alert-bot/1.0"},
    )
    urllib.request.urlopen(req, timeout=20).read()


def format_trade(t: dict) -> str:
    msg = (
        "🏛️ **New Pelosi trade filed**\n"
        f"**Stock:** {t['stock']}\n"
        f"**Transaction (type / size):** {t['transaction']}\n"
        f"**Traded:** {t['traded']}\n"
        f"**Filed:** {t['filed']}\n"
    )
    if t["description"]:
        msg += f"**Details:** {t['description'][:300]}\n"
    return msg + f"<{URL}>"


def main() -> int:
    trades = scrape()
    if not trades:
        print("No trades parsed. Page layout may have changed.", file=sys.stderr)
        return 1

    # Shared with monitor_overlap.py (same workflow run; not committed)
    Path("trades_pelosi.json").write_text(json.dumps(trades))

    if not SEEN_FILE.exists():
        SEEN_FILE.write_text(json.dumps([t["id"] for t in trades]))
        post_discord(f"✅ Pelosi trade tracker is live. Watching {len(trades)} existing trades; "
                     "you'll be pinged only for new ones.")
        print(f"Seeded {len(trades)} trades.")
        return 0

    seen = set(json.loads(SEEN_FILE.read_text()))
    new = [t for t in trades if t["id"] not in seen]
    print(f"{len(trades)} trades on page, {len(new)} new.")

    # The table lists newest first; alert oldest first.
    for t in reversed(new):
        post_discord(format_trade(t))
        seen.add(t["id"])
        SEEN_FILE.write_text(json.dumps(sorted(seen)))  # save as we go so a crash can't re-alert
        time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
