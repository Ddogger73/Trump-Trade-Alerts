"""Watches the Pure Power Picks Pelosi tracker and posts new trades to Discord.

The "Latest Trades" table is filled in by JavaScript (the raw page just says
"ACQUIRING FILINGS..."), so this uses a headless browser (Playwright).

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

URL = "https://purepowerpicks.com/nancy-pelosi-stock-tracker/"
SEEN_FILE = Path("seen_pelosi.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Finds the trades table by its headers, and checks it has real rows (not the
# "ACQUIRING FILINGS..." placeholder).
TABLE_READY_JS = """
() => {
  for (const t of document.querySelectorAll('table')) {
    const head = (t.querySelector('thead') || t).innerText.toLowerCase();
    if (!head.includes('asset') || !head.includes('action')) continue;
    const rows = [...t.querySelectorAll('tbody tr')];
    if (rows.length && rows.every(r => !r.innerText.toUpperCase().includes('ACQUIRING'))
        && rows[0].querySelectorAll('td').length >= 3) return true;
  }
  return false;
}
"""


def clean(text: str) -> str:
    text = re.sub(r"[ \t\u00a0]+", " ", text.strip())
    return re.sub(r"\s*\n\s*", " | ", text)


# Relative times like "3d ago" would change between runs and cause repeat alerts.
RELATIVE = re.compile(r"\b\d+\s*(?:s|m|h|d|w|min|mins|hr|hrs|hour|hours|day|days|week|weeks)\s*ago\b", re.I)


def scrape() -> list[dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA, viewport={"width": 1400, "height": 1000})
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_function(TABLE_READY_JS, timeout=60_000)
            page.wait_for_timeout(1_500)
        except Exception:
            page.screenshot(path="debug_pelosi.png", full_page=True)
            Path("debug_pelosi.html").write_text(page.content(), encoding="utf-8")
            raise

        table = page.locator("table", has_text="Asset").filter(has_text="Action").first
        trades = []
        rows = table.locator("tbody tr")
        for r in range(rows.count()):
            row = rows.nth(r)
            cells = [clean(c) for c in row.locator("td").all_inner_texts()]
            if len(cells) < 4:
                continue
            when, asset, action, amount = cells[:4]
            link = ""
            anchors = row.locator("a[href]")
            if anchors.count():
                link = anchors.first.get_attribute("href") or ""
            key = "|".join(RELATIVE.sub("", c) for c in (when, asset, action, amount, link))
            trades.append(
                {
                    "id": hashlib.sha1(key.encode()).hexdigest(),
                    "when": when,
                    "asset": asset,
                    "action": action,
                    "amount": amount,
                    "link": link,
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
        "🏛️ **New Pelosi trade disclosed**\n"
        f"**Asset:** {t['asset']}\n"
        f"**Action:** {t['action']}\n"
        f"**Amount:** {t['amount']}\n"
        f"**Traded / Filed:** {t['when']}\n"
    )
    if t["link"]:
        link = t["link"] if t["link"].startswith("http") else "https://purepowerpicks.com" + t["link"]
        msg += f"<{link}>\n"
    msg += f"<{URL}>"
    return msg


def main() -> int:
    trades = scrape()
    if not trades:
        print("No trades parsed. Page layout may have changed.", file=sys.stderr)
        return 1

    if not SEEN_FILE.exists():
        SEEN_FILE.write_text(json.dumps([t["id"] for t in trades]))
        post_discord(f"✅ Pelosi trade tracker is live. Watching {len(trades)} existing trades; "
                     "you'll be pinged only for new ones.")
        print(f"Seeded {len(trades)} trades.")
        return 0

    seen = set(json.loads(SEEN_FILE.read_text()))
    new = [t for t in trades if t["id"] not in seen]
    print(f"{len(trades)} trades on page, {len(new)} new.")

    # The feed lists newest first; alert oldest first.
    for t in reversed(new):
        post_discord(format_trade(t))
        seen.add(t["id"])
        SEEN_FILE.write_text(json.dumps(sorted(seen)))
        time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
