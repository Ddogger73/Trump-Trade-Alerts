"""Alerts Discord when 2+ of your tracked people trade the same stock in the same
direction (both bought, or both sold) within the last WINDOW_DAYS days.

It reads the trade lists the other trackers save during the same workflow run
(trades_trump.json, trades_pelosi.json, trades_davidson.json), so it needs no
extra scraping. It is a rule-based flag, not investment advice.

State is kept in seen_overlap.json. The first run records the overlaps that
already exist without alerting, then only NEW overlaps (or a new person joining
an existing one) trigger a message.
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

PEOPLE = {
    "Trump": "trades_trump.json",
    "Pelosi": "trades_pelosi.json",
    "Davidson": "trades_davidson.json",
}
SEEN_FILE = Path("seen_overlap.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
WINDOW_DAYS = 90   # only count trades from the last N days
MIN_PEOPLE = 2     # alert when at least this many people overlap
MAX_SEEN = 1000

TICKER_RE = re.compile(r"^\$?([A-Z]{1,5}(?:[.-][A-Z])?)$")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9}\.? \d{1,2}, \d{4}|\d{1,2}/\d{1,2}/\d{4}")
DATE_FORMATS = ["%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%b. %d, %Y", "%m/%d/%Y"]


def ticker_of(cell: str) -> str:
    parts = [p for p in re.split(r"\s*\|\s*|\s+", cell.strip()) if p]
    for p in parts[:3]:
        m = TICKER_RE.match(p)
        if m:
            return m.group(1)
    return parts[0].upper() if parts else ""


def direction_of(tx: str) -> str | None:
    t = tx.lower()
    if re.search(r"purchase|\bbuy|bought", t):
        return "buy"
    if re.search(r"\bsale|\bsell|\bsold", t):
        return "sell"
    return None  # exchanges etc. are ignored


def parse_date(s: str) -> date | None:
    m = DATE_RE.search(s or "")
    if not m:
        return None
    txt = m.group(0).replace("Sept", "Sep")
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(txt, fmt).date()
        except ValueError:
            pass
    return None


def in_window(trade: dict, today: date) -> bool:
    d = parse_date(trade.get("traded", "")) or parse_date(trade.get("filed", ""))
    return d is None or (today - d) <= timedelta(days=WINDOW_DAYS)


def find_overlaps(data: dict[str, list[dict]], today: date) -> dict:
    groups: dict[tuple[str, str], dict[str, list[dict]]] = {}
    for person, trades in data.items():
        for t in trades:
            tk, d = ticker_of(t.get("stock", "")), direction_of(t.get("transaction", ""))
            if tk and d and in_window(t, today):
                groups.setdefault((tk, d), {}).setdefault(person, []).append(t)
    return {k: v for k, v in groups.items() if len(v) >= MIN_PEOPLE}


def overlap_key(group: tuple[str, str], people: dict) -> str:
    return f"{group[0]}|{group[1]}|{'+'.join(sorted(people))}"


def format_alert(group: tuple[str, str], people: dict, total: int) -> str:
    tk, d = group
    verb = "bought" if d == "buy" else "sold"
    lines = [f"🤝 **Overlap: {len(people)} of {total} tracked people {verb} {tk}**"]
    for person in sorted(people):
        for t in people[person][:3]:
            detail = t.get("transaction", "")
            when = f"traded {t.get('traded', '?')}, filed {t.get('filed', '?')}"
            lines.append(f"• **{person}**: {detail} ({when})")
    lines.append("")
    lines.append("⚠️ Filings run up to ~45 days behind the actual trades, so the price may have "
                 "moved. This is a flag from public filings, not investment advice.")
    return "\n".join(lines)[:1900]


def post_discord(content: str) -> None:
    if not WEBHOOK:
        print("DISCORD_WEBHOOK_URL not set; would have sent:\n" + content)
        return
    req = urllib.request.Request(
        WEBHOOK,
        data=json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "trade-alert-bot/1.0"},
    )
    urllib.request.urlopen(req, timeout=20).read()


def load_data() -> dict[str, list[dict]]:
    data = {}
    for person, fname in PEOPLE.items():
        p = Path(fname)
        if p.exists():
            data[person] = json.loads(p.read_text())
        else:
            print(f"No data for {person} this run ({fname} missing); skipping them.")
    return data


def main() -> int:
    data = load_data()
    today = date.today()
    first_run = not SEEN_FILE.exists()

    if first_run and len(data) < len(PEOPLE):
        print("First run needs data from every tracker. Make sure the other steps succeeded.",
              file=sys.stderr)
        return 1
    if len(data) < MIN_PEOPLE:
        return 0

    overlaps = find_overlaps(data, today)
    alerted = [] if first_run else json.loads(SEEN_FILE.read_text())["alerted"]
    alerted_set = set(alerted)

    if first_run:
        keys = [overlap_key(g, p) for g, p in overlaps.items()]
        SEEN_FILE.write_text(json.dumps({"alerted": keys[-MAX_SEEN:]}))
        summary = ", ".join(
            f"{g[0]} ({g[1]}: {' + '.join(sorted(p))})" for g, p in list(overlaps.items())[:10]
        ) or "none right now"
        post_discord(f"✅ Overlap tracker is live. Current overlaps in the last {WINDOW_DAYS} days: "
                     f"{summary}. You'll be pinged when a new one forms.")
        print(f"Seeded {len(keys)} existing overlaps.")
        return 0

    new = [(g, p) for g, p in overlaps.items() if overlap_key(g, p) not in alerted_set]
    print(f"{len(overlaps)} current overlaps, {len(new)} new.")
    for group, people in new:
        post_discord(format_alert(group, people, len(PEOPLE)))
        alerted.append(overlap_key(group, people))
        SEEN_FILE.write_text(json.dumps({"alerted": alerted[-MAX_SEEN:]}))
        time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
