"""Alerts Discord when Donald Trump's Truth Social posts mention watched keywords.

Source: the public RSS feed of Trump's Truth (trumpstruth.org), an independent
archive of his Truth Social posts. This does NOT touch Truth Social itself.

State is kept in seen_truth.json. The first run records the posts currently in
the feed without alerting, then only NEW posts that match are sent.

Edit the KEYWORDS / TICKERS / COMPANIES lists below any time (click the pencil
icon on GitHub). No other change is needed.
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

FEED_URL = "https://www.trumpstruth.org/feed"
SEEN_FILE = Path("seen_truth.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
MAX_SEEN = 500        # how many post IDs to remember
FAIL_WARN_AT = 12     # consecutive failed checks (~1 hour) before a Discord warning
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ---------------------------------------------------------------- EDIT BELOW
# Matches the START of a word, any ending: "invest" also catches investing,
# investment, investors; "tariff" catches tariffs. Not case sensitive.
KEYWORDS = ["invest", "buy", "sell", "stock", "tariff", "deal"]

# Tickers: matched as whole words, UPPERCASE only (so "META" matches, "meta"
# doesn't). Cashtags like $TSLA are always matched for any symbol.
TICKERS = [
    "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "INTC", "AMD",
    "AVGO", "DJT", "PLTR", "COIN", "JPM", "BAC", "WFC", "PFE", "MRNA", "LLY", "UNH",
    "JNJ", "NFLX", "DIS", "ORCL", "TSM", "SMCI", "LMT", "RTX", "NOC", "XOM", "CVX",
    "OXY", "WMT", "COST", "PYPL", "UBER", "SPY", "QQQ",
]

# Company names: matched as whole words/phrases, not case sensitive.
COMPANIES = [
    "Nvidia", "Apple", "Microsoft", "Amazon", "Alphabet", "Google", "Facebook",
    "Tesla", "Intel", "Boeing", "Lockheed", "Raytheon", "Northrop", "Exxon",
    "Chevron", "Occidental", "Walmart", "Costco", "JPMorgan", "Goldman Sachs",
    "Bank of America", "Wells Fargo", "Citigroup", "Pfizer", "Moderna", "Eli Lilly",
    "UnitedHealth", "Johnson & Johnson", "Netflix", "Disney", "Oracle", "Palantir",
    "Coinbase", "Micron", "Broadcom", "TSMC", "Taiwan Semiconductor",
    "General Motors", "Trump Media", "U.S. Steel", "Nippon Steel",
]
# ---------------------------------------------------------------- EDIT ABOVE

KW_RE = re.compile(r"\b(?:%s)\w*" % "|".join(re.escape(k) for k in KEYWORDS), re.I)
TICKER_RE = re.compile(r"(?<![A-Za-z0-9])\$?(?:%s)(?![A-Za-z0-9])" % "|".join(TICKERS))
CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9])\$[A-Z]{1,5}\b")
COMPANY_RE = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(c) for c in COMPANIES), re.I)


def find_matches(text: str) -> list[str]:
    found: list[str] = []
    for rx in (KW_RE, TICKER_RE, CASHTAG_RE, COMPANY_RE):
        for m in rx.finditer(text):
            w = m.group(0).strip()
            if w.lower() not in (f.lower() for f in found):
                found.append(w)
    return found[:10]


# ------------------------------------------------------------------- feed
def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", raw or "")
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t\u00a0]+", " ", raw)
    return re.sub(r"\n\s*\n+", "\n", raw).strip()


def fetch_feed() -> bytes:
    req = urllib.request.Request(
        FEED_URL,
        headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def parse_items(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        f: dict[str, str] = {}
        for child in el:
            name = local(child.tag)
            val = (child.text or "").strip()
            if name == "link" and not val:
                val = child.attrib.get("href", "")
            f.setdefault(name, val)
            if name == "encoded":  # content:encoded wins as the full body
                f["encoded"] = val
        title = html_to_text(f.get("title", ""))
        body = html_to_text(f.get("encoded") or f.get("description") or f.get("content") or f.get("summary") or "")
        link = f.get("link", "")
        posted = f.get("pubdate") or f.get("published") or f.get("updated") or ""
        text = body or title
        searchable = "\n".join(dict.fromkeys(t for t in (title, body) if t))
        pid = f.get("guid") or f.get("id") or link or hashlib.sha1(searchable.encode()).hexdigest()
        items.append({"id": pid, "text": text, "search": searchable, "link": link, "posted": posted})
    return items  # feed order = newest first


# ---------------------------------------------------------------- discord
def post_discord(content: str) -> None:
    if not WEBHOOK:
        print("DISCORD_WEBHOOK_URL not set; would have sent:\n" + content)
        return
    req = urllib.request.Request(
        WEBHOOK,
        # allowed_mentions stops a post containing @everyone from pinging anyone
        data=json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "trade-alert-bot/1.0"},
    )
    urllib.request.urlopen(req, timeout=20).read()


def format_alert(item: dict, matches: list[str]) -> str:
    text = item["text"]
    if len(text) > 1500:
        text = text[:1500].rstrip() + "…"
    quoted = "\n".join("> " + ln for ln in text.splitlines() if ln.strip())
    msg = f"🚨 **Trump Truth Social post. Matched: {', '.join(matches)}**\n{quoted}\n"
    if item["posted"]:
        msg += f"🕒 {item['posted']}\n"
    if item["link"]:
        msg += f"<{item['link']}>"
    return msg.strip()


# ------------------------------------------------------------------ state
def load_state() -> dict | None:
    if not SEEN_FILE.exists():
        return None
    return json.loads(SEEN_FILE.read_text())


def save_state(state: dict) -> None:
    new = json.dumps(state)
    if not SEEN_FILE.exists() or SEEN_FILE.read_text() != new:
        SEEN_FILE.write_text(new)


def main() -> int:
    state = load_state()

    try:
        items = parse_items(fetch_feed())
        if not items:
            raise ValueError("feed contained no posts")
    except Exception as exc:  # network error, block, layout change...
        print(f"Feed check failed: {exc!r}", file=sys.stderr)
        if state is None:
            return 1  # first run: fail loudly so you see it
        # Later runs: don't fail (that would email you every 5 minutes).
        # Warn once in Discord if it keeps failing.
        state["fails"] = state.get("fails", 0) + 1
        if state["fails"] == FAIL_WARN_AT:
            post_discord("⚠️ Truth Social tracker can't read the feed (failing for about an hour). "
                         "Check the Actions tab.")
        save_state(state)
        return 0

    if state is None:
        save_state({"seen": [i["id"] for i in reversed(items)][-MAX_SEEN:], "fails": 0})
        post_discord(f"✅ Truth Social tracker is live. Watching {len(items)} recent posts; you'll be "
                     "pinged for new posts mentioning your keywords or tickers.")
        print(f"Seeded {len(items)} posts.")
        return 0

    seen = state.get("seen", [])
    seen_set = set(seen)
    new = [i for i in items if i["id"] not in seen_set]
    print(f"{len(items)} posts in feed, {len(new)} new.")

    state["fails"] = 0
    for item in reversed(new):  # oldest first
        matches = find_matches(item["search"])
        if matches:
            post_discord(format_alert(item, matches))
            time.sleep(1)
        seen.append(item["id"])
        state["seen"] = seen[-MAX_SEEN:]
        save_state(state)  # save as we go so a crash can't re-alert
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
