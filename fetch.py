"""
Pull every feed in feeds.yaml, filter to AI / startup stories, deduplicate,
and write news.json.

    pip install feedparser pyyaml
    python fetch.py

Output goes to news.json in the current directory. The static page reads
that file; nothing else touches the feeds.

Note on excerpts: summaries are capped at EXCERPT_CHARS and always carry a
link back to the original. This is a link digest, not a mirror. Do not
raise that cap to republish whole articles.
"""

import hashlib
import html
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import feedparser
import yaml

# --- tuning knobs -------------------------------------------------------

MAX_AGE_DAYS = 3        # refreshes every 6h, so older items are just clutter
MAX_ITEMS = 120         # cap on the final list
EXCERPT_CHARS = 280
UA = "Mozilla/5.0 (compatible; newsletter-bot/0.1)"

# Tracking params to strip before comparing URLs.
JUNK_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
               "utm_content", "ref", "fbclid", "gclid", "mc_cid", "mc_eid"}

# A story is relevant if its feed category tags OR its title/summary match.
KEYWORDS = {
    "ai", "artificial intelligence", "machine learning", "llm", "genai",
    "generative ai", "neural", "openai", "anthropic", "deepmind", "gemini",
    "claude", "chatgpt", "gpt", "mistral", "hugging face", "nvidia", "gpu",
    "agent", "agentic", "model", "inference", "transformer", "robotics",
    "startup", "founder", "seed round", "series a", "series b", "series c",
    "funding", "raises", "raised", "valuation", "venture", "vc", "unicorn",
    "acquisition", "acquires", "ipo", "yc", "y combinator", "accelerator",
}

# Tiers whose items are always kept, regardless of keyword match. These are
# already narrow enough that filtering would only throw away good material.
ALWAYS_KEEP_TIERS = {"ai_news", "labs", "authors"}


# --- helpers ------------------------------------------------------------

def clean_text(raw):
    """Strip HTML tags and entities, collapse whitespace."""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def excerpt(raw):
    text = clean_text(raw)
    if len(text) <= EXCERPT_CHARS:
        return text
    cut = text[:EXCERPT_CHARS].rsplit(" ", 1)[0]
    return cut + "\u2026"


def canonical_url(url):
    """Strip tracking params and trailing slash so the same story matches."""
    try:
        parts = urlparse(url)
    except ValueError:
        return url
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in JUNK_PARAMS]
    return urlunparse((
        parts.scheme, parts.netloc.lower().removeprefix("www."),
        parts.path.rstrip("/"), "", urlencode(query), "",
    ))


def title_fingerprint(title):
    """Loose key so the same story from five outlets collapses to one."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    meaningful = [w for w in words if len(w) > 3][:8]
    return " ".join(sorted(meaningful))


def published_at(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


def is_relevant(title, summary, tags, tier_name):
    if tier_name in ALWAYS_KEEP_TIERS:
        return True
    haystack = f"{title} {summary} {' '.join(tags)}".lower()
    return any(k in haystack for k in KEYWORDS)


# --- main ---------------------------------------------------------------

def collect(feed, tier):
    """Fetch one feed, return (items, error_or_None)."""
    parsed = feedparser.parse(feed["url"], agent=UA)

    if parsed.get("status") and parsed["status"] >= 400:
        return [], f"HTTP {parsed['status']}"
    if not parsed.entries:
        return [], "no entries"

    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    items = []

    for entry in parsed.entries:
        when = published_at(entry)
        if when is None or when < cutoff:
            continue

        title = clean_text(entry.get("title", ""))
        link = entry.get("link", "")
        if not title or not link:
            continue

        tags = [t.get("term", "") for t in entry.get("tags", []) if t.get("term")]
        body = entry.get("summary", "") or entry.get("description", "")
        summary = excerpt(body)

        if not is_relevant(title, summary, tags, tier["name"]):
            continue

        canonical = canonical_url(link)
        items.append({
            "id": hashlib.sha1(canonical.encode()).hexdigest()[:12],
            "title": title,
            "url": link,
            "canonical": canonical,
            "source": feed["title"],
            "source_id": feed["id"],
            "author": clean_text(entry.get("author", "")) or None,
            "published": when.isoformat(),
            "summary": summary,
            "tags": tags[:6],
            "tier": tier["name"],
            "region": tier["region"],
        })

    return items, None


def deduplicate(items):
    """Newest wins on exact URL; first-seen wins on near-identical titles."""
    items.sort(key=lambda i: i["published"], reverse=True)

    seen_urls, seen_titles, kept = set(), set(), []
    for item in items:
        fingerprint = title_fingerprint(item["title"])
        if item["canonical"] in seen_urls or fingerprint in seen_titles:
            continue
        seen_urls.add(item["canonical"])
        if fingerprint:
            seen_titles.add(fingerprint)
        kept.append(item)
    return kept


def main(path="feeds.yaml"):
    with open(path) as handle:
        config = yaml.safe_load(handle)

    all_items, ok, failed = [], 0, []

    for tier in config["tiers"]:
        for feed in tier["feeds"]:
            try:
                items, error = collect(feed, tier)
            except Exception as exc:
                items, error = [], str(exc)

            if error:
                failed.append({"source": feed["title"], "error": error})
                print(f"  fail  {feed['title']}: {error}", file=sys.stderr)
            else:
                ok += 1
                print(f"  ok    {feed['title']}: {len(items)} kept")
            all_items.extend(items)

    kept = deduplicate(all_items)
    dropped = len(all_items) - len(kept)
    kept = kept[:MAX_ITEMS]

    for item in kept:
        item.pop("canonical", None)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources_ok": ok,
        "sources_failed": failed,
        "item_count": len(kept),
        "items": kept,
    }

    with open("news.json", "w") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)

    print(f"\n{len(kept)} items written to news.json "
          f"({dropped} duplicates removed, {len(failed)} feeds failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "feeds.yaml"))
