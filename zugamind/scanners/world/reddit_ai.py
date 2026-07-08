"""Reddit AI scanner — surfaces top posts from AI/ML subreddits.

Hits Reddit's public RSS feeds (no auth, no key). Cached for 1h since
new posts come in steadily but the sentinel cycle is every ~7 min.

Subreddits chosen for AI implementation inspiration:
  - r/MachineLearning   (research + papers)
  - r/LocalLLaMA        (open-weights, self-hostable models)
  - r/singularity       (broader AI news + speculation)
"""

import json
import re
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

_SUBS = ["MachineLearning", "LocalLLaMA", "singularity"]
_FEED_URL = "https://www.reddit.com/r/{sub}/hot/.rss?limit=8"
_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_CACHE_TTL_SEC = 60 * 60
_TIMEOUT_SEC = 5

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _cache_path() -> Path:
    cache_dir = Path(__file__).resolve().parent.parent.parent / "data" / "scanner_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "reddit_ai.json"


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
    posts: list[dict] = []
    for sub in _SUBS:
        try:
            posts.extend(_fetch_sub(sub)[:4])  # top 4 per sub
        except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError, TimeoutError, OSError):
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
    except Exception:
        pass
    if not use_cache:
        posts = _fetch_all()
        if posts:
            try:
                cache.write_text(json.dumps(posts), encoding="utf-8")
            except OSError:
                pass

    triggers: list[dict] = []
    for p in posts[:6]:  # cap at 6 across subs
        slug = (p.get("id") or p.get("link") or "")[-40:]
        triggers.append(
            {
                "type": "reddit_ai_post",
                "detail": f"r/{p.get('sub','?')}: {p.get('title','?')}",
                "novelty": 0.75,
                "relevance": 0.55,
                "urgency": 0.25,
                "post_slug": slug,
                "post_url": p.get("link", ""),
                "subreddit": p.get("sub", ""),
            }
        )
    return triggers
