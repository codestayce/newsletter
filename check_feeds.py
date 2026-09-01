"""
Check every feed in feeds.yaml: is it alive, and how fresh is it?

    pip install feedparser pyyaml
    python check_feeds.py

Run this before you write any pipeline code. A feed that 404s or has not
updated in six months is the thing that quietly breaks the newsletter.
"""

import sys
from datetime import datetime, timezone

import feedparser
import yaml

TIMEOUT = 20
UA = "Mozilla/5.0 (compatible; newsletter-bot/0.1)"


def age_in_days(entry):
    """Days since this entry was published, or None if no usable date."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    published = datetime(*parsed[:6], tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - published).days


def check(feed):
    """Fetch one feed and return (symbol, message)."""
    try:
        parsed = feedparser.parse(feed["url"], agent=UA)
    except Exception as exc:
        return "FAIL", f"could not fetch: {exc}"

    status = parsed.get("status")
    if status and status >= 400:
        return "FAIL", f"HTTP {status}"

    if not parsed.entries:
        reason = getattr(parsed, "bozo_exception", "no entries returned")
        return "FAIL", str(reason)

    days = age_in_days(parsed.entries[0])
    count = len(parsed.entries)

    if days is None:
        return "WARN", f"{count} entries, no date on latest"
    if days > 90:
        return "WARN", f"{count} entries, newest is {days} days old"
    return "OK", f"{count} entries, newest {days}d ago"


def main(path="feeds.yaml"):
    with open(path) as handle:
        config = yaml.safe_load(handle)

    failures = []

    for tier in config["tiers"]:
        print(f"\n{tier['name']}  ({tier['region']})")
        print("-" * 60)

        for feed in tier["feeds"]:
            symbol, message = check(feed)
            print(f"  [{symbol:4}] {feed['title']:<34} {message}")
            if symbol == "FAIL":
                failures.append((feed["title"], feed["url"], message))

    print()
    if failures:
        print(f"{len(failures)} feed(s) need attention:\n")
        for title, url, message in failures:
            print(f"  {title}\n    {url}\n    {message}\n")
        print("Try /feed, /rss, /feed.xml or /atom.xml on the site root,")
        print("or view source and search for application/rss+xml.")
    else:
        print("All feeds returned recent items.")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "feeds.yaml"))
