"""Reddit AI scanner — surfaces top posts from AI/ML subreddits.

Hits Reddit's public RSS feeds (no auth, no key). Cached for 1h since
new posts come in steadily but the sentinel cycle is every ~7 min.

Best-effort and unofficial: Reddit rate-limits and sometimes blocks
unauthenticated automated access, so this feed may break or go dark
without notice. Volume here is deliberately tiny (3 subreddits, 8 items,
1h cache) and the scanner fails silent to an empty list — swap in an
OAuth-based fetcher if you need a guaranteed feed.

Subreddits chosen for AI implementation inspiration:
  - r/MachineLearning   (research + papers)
  - r/LocalLLaMA        (open-weights, self-hostable models)
  - r/singularity       (broader AI news + speculation)

A post triggers ONCE, ever (scanners.seen_items). /hot is not a queue of new
things — a stickied post sits at the top of it indefinitely, and before this
scanner remembered what it had emitted, engine habituation was the only thing
between it and the workspace. Habituation forgets after HABITUATION_HOURS, so
r/singularity's "Discord Server Link" won the global workspace twelve times
between Aug 9 and Aug 18, r/MachineLearning's self-promotion thread nine, and
r/LocalLLaMA's monthly megathread eight. None of them were ever news.
"""

import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

from foundation.fs import atomic_write_text

from scanners.seen_items import read_seen, write_seen

logger = logging.getLogger("zugamind.scanners.reddit_ai")

_SUBS = ["MachineLearning", "LocalLLaMA", "singularity"]
_FEED_URL = "https://www.reddit.com/r/{sub}/hot/.rss?limit=8"
_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_CACHE_TTL_SEC = 60 * 60
_TIMEOUT_SEC = 5
_SEEN_MAX = 600  # three subs x eight items per sweep; this holds many weeks

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _cache_path() -> Path:
    # Honors ZUGAMIND_DATA_DIR without importing foundation — scanners stay standalone.
    data_dir = Path(os.environ.get("ZUGAMIND_DATA_DIR")
                    or Path(__file__).resolve().parent.parent.parent / "data")
    cache_dir = data_dir / "scanner_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "reddit_ai.json"


def _seen_path() -> Path:
    return _cache_path().parent / "reddit_ai_seen.json"


def _post_key(post: dict) -> str:
    """Stable identity for one post: reddit's own id, else its permalink."""
    return str(post.get("id") or post.get("link") or post.get("title") or "")


def _fetch_sub(sub: str) -> list[dict]:
    url = _FEED_URL.format(sub=sub)
    req = urllib.request.Request(url, headers={"User-Agent": "ZugaMind/1.0 (read-only)"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    root = ET.fromstring(text)
    out: list[dict] = []
    for entry in root.findall(f"{_ATOM_NS}entry"):
        title = (entry.findtext(f"{_ATOM_NS}title") or "").strip()
        link_el = entry.find(f"{_ATOM_NS}link")
        link = link_el.get("href") if link_el is not None else ""
        ident = (entry.findtext(f"{_ATOM_NS}id") or "").strip()
        if not title:
            continue
        out.append({"sub": sub, "title": title[:240], "link": link, "id": ident})
    return out


def _fetch_all() -> list[dict]:
    # Reddit's anon quota is ~1 req/IP per ~30s window, so back-to-back fetches
    # meant the last sub in the list 429'd on every poll. Rotate which sub goes
    # first (hour-based, deterministic) and sleep the actual documented window
    # between requests so no sub is permanently starved. This only costs
    # wall-clock on a cache MISS (once per _CACHE_TTL_SEC), never on a hit.
    posts: list[dict] = []
    order = list(_SUBS)
    shift = int(time.time() // 3600) % len(order)
    order = order[shift:] + order[:shift]
    for i, sub in enumerate(order):
        if i:
            time.sleep(30)
        try:
            posts.extend(_fetch_sub(sub)[:4])  # top 4 per sub
        except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError, TimeoutError, OSError) as e:
            logger.debug("reddit_ai fetch failed for r/%s: %s", sub, e)
            continue
    return posts


def scan_reddit_ai() -> list[dict]:
    """Return triggers for hot AI/ML reddit posts."""
    cache = _cache_path()
    posts: list[dict] = []
    use_cache = False
    try:
        if cache.exists() and (time.time() - cache.stat().st_mtime) < _CACHE_TTL_SEC:
            posts = json.loads(cache.read_text(encoding="utf-8"))
            use_cache = True
    except Exception as e:  # corrupt cache is non-fatal — fall back to live fetch
        logger.debug("reddit_ai cache load failed (ignoring): %s", e)
    if not use_cache:
        posts = _fetch_all()
        if posts:
            try:
                atomic_write_text(cache, json.dumps(posts))
            except OSError as e:  # persistence best-effort — never break the cycle
                logger.debug("reddit_ai cache save failed (non-fatal): %s", e)

    # Brand watch (same contract as the HN scanner): ZUGAMIND_BRAND_TERMS
    # comma-list, unset = off. A brand mention in a title outranks ambient
    # subreddit chatter but stays below the alarm lane.
    brand_terms = [t.strip() for t in
                   os.environ.get("ZUGAMIND_BRAND_TERMS", "").split(",") if t.strip()]
    brand_re = (re.compile("|".join(re.escape(t) for t in brand_terms), re.IGNORECASE)
                if brand_terms else None)

    if not posts:
        return []  # feed dark: stay cold rather than baseline an empty sweep

    seen = read_seen(_seen_path())
    if seen is None:
        # Cold start: baseline the sweep silently. /hot is mostly days old at
        # any moment, so emitting it on first run would hand the workspace a
        # backlog and call it news.
        write_seen(_seen_path(), {_post_key(p): time.time() for p in posts}, _SEEN_MAX)
        return []

    now = time.time()
    fresh = [p for p in posts if _post_key(p) not in seen]

    triggers: list[dict] = []
    for p in fresh[:6]:  # cap at 6 across subs
        slug = (p.get("id") or p.get("link") or "")[-40:]
        brand_hit = bool(brand_re and brand_re.search(p.get("title", "") or ""))
        seen[_post_key(p)] = now
        triggers.append(
            {
                "type": "reddit_ai_post",
                "detail": (f"r/{p.get('sub','?')} BRAND MENTION: {p.get('title','?')}"
                           if brand_hit else
                           f"r/{p.get('sub','?')}: {p.get('title','?')}"),
                "novelty": 0.9 if brand_hit else 0.75,
                "relevance": 0.9 if brand_hit else 0.55,
                "urgency": 0.75 if brand_hit else 0.25,
                **({"brand_mention": True} if brand_hit else {}),
                "post_slug": slug,
                "post_url": p.get("link", ""),
                "subreddit": p.get("sub", ""),
            }
        )

    if triggers:
        # protect = every key still visible on the feed this sweep. The
        # stored timestamp is FIRST-seen and never refreshed, so a
        # stickied post holds the oldest stamp in the set and is the
        # first thing evicted when the cap trips — then it re-fires.
        write_seen(_seen_path(), seen, _SEEN_MAX,
                   protect={_post_key(p) for p in posts})
    return triggers
