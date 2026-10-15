"""Watches Quiver's Berkshire Hathaway (Warren Buffett) 13F page and posts to
Discord when a NEW quarterly filing appears.

Unlike the politician trackers, Berkshire doesn't report individual trades. It
files a 13F once per quarter listing everything it holds, and Quiver shows the
change since the previous quarter. So this alerts per filing, with a summary of
new positions, exits, and increases/decreases.

The holdings table is loaded by JavaScript, so a headless browser (Playwright)
renders the page first.

State is kept in seen_buffett.json. The first run records the current filing
without alerting about it (it just posts a "live" message).
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

URL = "https://www.quiverquant.com/institutions/BERKSHIRE%20HATHAWAY%20INC/"
SEEN_FILE = Path("seen_buffett.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

TABLE_READY_JS = """
() => {
  for (const t of document.querySelectorAll('table')) {
    const head = (t.querySelector('thead') || t).innerText.toLowerCase();
    if (!head.includes('holdings') || !head.includes('change')) continue;
    const rows = [...t.querySelectorAll('tbody tr')];
    if (rows.length && rows[0].querySelectorAll('td').length >= 5
        && !rows[0].innerText.toLowerCase().includes('loading')) return true;
  }
  return false;
}
"""

DATE = r"[A-Za-z]+\.? \d{1,2}, \d{4}"


def clean(text: str) -> str:
    text = re.sub(r"[ \t\u00a0]+", " ", text.strip())
    return re.sub(r"\s*\n\s*", " | ", text)


def scrape() -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA, viewport={"width": 1400, "height": 1000})
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_function(TABLE_READY_JS, timeout=60_000)
            page.wait_for_timeout(2_000)
        except Exception:
            page.screenshot(path="debug_buffett.png", full_page=True)
            Path("debug_buffett.html").write_text(page.content(), encoding="utf-8")
            raise

        body = re.sub(r"\s+", " ", page.inner_text("body"))
        m_period = re.search(rf"positions held on ({DATE})", body)
        m_filed = re.search(rf"was filed on ({DATE})", body)
        period = m_period.group(1) if m_period else ""
        filed = m_filed.group(1).rstrip(".") if m_filed else ""

        table = (
            page.locator("table")
            .filter(has=page.locator("th", has_text="Holdings"))
            .filter(has=page.locator("th", has_text="Change"))
            .first
        )
        headers = [clean(h).lower() for h in table.locator("thead th").all_inner_texts()]

        def col(default: int, *needles: str) -> int:
            for i, h in enumerate(headers):
                if all(n in h for n in needles):
                    return i
            return default

        i_stock = col(0, "stock")
        i_shares = col(1, "holdings", "share")
        i_usd = col(2, "holdings", "usd")
        i_chg_sh = col(4, "change", "share")
        i_chg_usd = col(5, "change", "usd")
        i_chg_pct = col(6, "% change")

        def get(cells, i):
            return cells[i] if 0 <= i < len(cells) else ""

        holdings = []
        rows = table.locator("tbody tr")
        for r in range(rows.count()):
            cells = [clean(c) for c in rows.nth(r).locator("td").all_inner_texts()]
            if len(cells) < 5 or not get(cells, i_stock):
                continue
            holdings.append(
                {
                    "stock": get(cells, i_stock),
                    "shares": get(cells, i_shares),
                    "usd": get(cells, i_usd),
                    "chg_shares": get(cells, i_chg_sh),
                    "chg_usd": get(cells, i_chg_usd),
                    "chg_pct": get(cells, i_chg_pct),
                }
            )
        browser.close()

    if period and filed:
        filing_id = f"{period}|{filed}"
    else:  # fall back to a fingerprint of the share counts
        sig = "|".join(sorted(f"{h['stock']}:{h['shares']}" for h in holdings))
        filing_id = "sig:" + hashlib.sha1(sig.encode()).hexdigest()
    return {"filing_id": filing_id, "period": period, "filed": filed, "holdings": holdings}


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


def post_long(text: str, limit: int = 1900) -> None:
    """Discord caps messages at 2000 chars, so split on line boundaries."""
    chunk = ""
    for line in text.split("\n"):
        if chunk and len(chunk) + len(line) + 1 > limit:
            post_discord(chunk)
            time.sleep(1)
            chunk = ""
        chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        post_discord(chunk)


def is_decrease(cell: str) -> bool:
    return any(ch in cell for ch in "-\u2212(")


def build_message(state: dict, filing: dict) -> str:
    holdings = filing["holdings"]
    prev = set(state.get("stocks", []))
    cur = {h["stock"]: h for h in holdings}
    new_pos = [h for h in holdings if h["stock"] not in prev]
    exited = [s for s in state.get("stocks", []) if s not in cur]
    changed = [
        h for h in holdings
        if h["stock"] in prev and re.search(r"[1-9]", h["chg_shares"])
    ]

    lines = [
        "📊 **Berkshire Hathaway filed a new 13F**",
        f"Report period: {filing['period'] or '?'} | Filed: {filing['filed'] or '?'} "
        f"| Positions: {len(holdings)}",
        "",
    ]
    if new_pos:
        lines.append("**🆕 New positions**")
        lines += [f"• {h['stock']}: {h['shares']} shares ({h['usd']})" for h in new_pos]
        lines.append("")
    if exited:
        lines.append("**🚪 Exited (no longer listed)**")
        lines += [f"• {s}" for s in exited]
        lines.append("")
    if changed:
        lines.append("**🔄 Changed positions**")
        for h in changed:
            arrow = "▼" if is_decrease(h["chg_shares"]) else "▲"
            lines.append(
                f"{arrow} {h['stock']}: {h['chg_shares']} shares "
                f"({h['chg_pct']}, {h['chg_usd']})"
            )
        lines.append("")
    if not (new_pos or exited or changed):
        lines.append("No position changes detected vs. the previous quarter.")
        lines.append("")
    lines.append(f"<{URL}>")
    return "\n".join(lines)


def main() -> int:
    filing = scrape()
    if not filing["holdings"]:
        print("No holdings parsed. Page layout may have changed.", file=sys.stderr)
        return 1

    state_new = {
        "filing_id": filing["filing_id"],
        "stocks": [h["stock"] for h in filing["holdings"]],
    }

    if not SEEN_FILE.exists():
        SEEN_FILE.write_text(json.dumps(state_new))
        post_discord(
            "✅ Berkshire Hathaway 13F tracker is live. Latest filing: report period "
            f"{filing['period'] or '?'}, filed {filing['filed'] or '?'}, "
            f"{len(filing['holdings'])} positions. You'll be pinged when the next one posts."
        )
        print("Seeded current filing.")
        return 0

    state = json.loads(SEEN_FILE.read_text())
    if state.get("filing_id") == filing["filing_id"]:
        print("No new filing.")
        return 0

    print(f"New filing detected: {filing['filing_id']}")
    post_long(build_message(state, filing))
    SEEN_FILE.write_text(json.dumps(state_new))  # only after the alert succeeded
    return 0


if __name__ == "__main__":
    sys.exit(main())
