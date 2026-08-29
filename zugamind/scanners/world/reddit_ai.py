"""Reddit AI scanner — surfaces top posts from AI/ML subreddits.

Hits Reddit's public JSON listing (no auth, no key) through scanners.safe_http
-- conditional GET, 429/Retry-After backoff, gzip and BOM-tolerant decoding,
and credential-safe redirects, none of which the old bare urlopen() + Atom-
feed/regex-HTML-strip parser had. Cached for 1h since new posts come in
steadily but the sentinel cycle is every ~7 min.

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

Per-sub fetch state (audit 2026-08-29): the old cache was a bare JSON list of
posts, freshness judged by the FILE'S OWN mtime, and only rewritten `if
posts:`. Once every sub in a sweep came back empty (a genuinely dark feed),
the file was never touched again, so its mtime froze -- and "now - mtime <
TTL" then read as permanently stale on every following call, re-paying the
full 30s x 2 stagger-sleep budget on the SYNCHRONOUS perception path every
~7-minute cycle, forever (measured against a 420s daemon interval). Fixed by
tracking `last_fetched` PER SUBREDDIT and always persisting it -- even an
all-dark sweep now costs one fetch attempt per sub per TTL window, not one
full sleep budget per cycle.
"""

import json
import logging
import os
import re
import time
from pathlib import Path

from foundation.fs import atomic_write_text
from foundation.text_format import truncate_title

import xml.etree.ElementTree as ET

from scanners import safe_http
from scanners.seen_items import read_seen, write_seen

logger = logging.getLogger("zugamind.scanners.reddit_ai")

_SUBS = ["MachineLearning", "LocalLLaMA", "singularity"]
# .rss, NOT .json: the JSON listing answers 403 Blocked for every
# User-Agent (probed 2026-08-29), while this one returns 200 with eight
# entries. The UA below matters too -- the previous "ZugaMind/scanner"
# string 429s on this same feed.
_FEED_URL = "https://www.reddit.com/r/{sub}/hot/.rss?limit=8"
_USER_AGENT = "ZugaMind/1.0 (read-only)"
_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_CACHE_TTL_SEC = 60 * 60
_TIMEOUT_SEC = 5
_SEEN_MAX = 600  # three subs x eight items per sweep; this holds many weeks

# `detail` is the literal briefing text handed to a paid model, and a title
# is third-party, untrusted text -- a newline or control byte inside it is a
# prompt-injection seam, not cosmetic noise. Collapse to single spaces FIRST,
# then hand the result to truncate_title, so a title padded with control
# characters can't dodge the length cut by hiding real content past it.
_CONTROL_WS_RE = re.compile(r"[\s\x00-\x1f\x7f]+")


def _clean_title(title: str) -> str:
    return truncate_title(_CONTROL_WS_RE.sub(" ", title or "").strip(), 240)


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


def _load_state() -> dict:
    """Per-sub fetch state: {"subs": {sub: {"last_fetched": epoch,
    "posts": [...], "etag": ..., "last_modified": ..., "fails": ...}}}.

    The nested per-sub dict doubles as the `state` safe_http.fetch_json
    persists its own etag/last_modified/fails/blocked_until into, so
    conditional-GET validators live right next to the bookkeeping that
    replaced the old whole-file mtime TTL.

    A cache file in the OLD bare-list shape (pre this fix) is read once as a
    one-shot seed rather than discarded and misread as a cold start -- the
    very next sweep sees empty per-sub state, fetches for real, and
    overwrites the file in the new shape.
    """
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except Exception as e:  # missing/corrupt cache is non-fatal — fall back to live fetch
        logger.debug("reddit_ai cache load failed (ignoring): %s", e)
        return {"subs": {}}
    if isinstance(data, dict) and isinstance(data.get("subs"), dict):
        return data
    if isinstance(data, list):
        return {"subs": {}, "_legacy_posts": data}
    return {"subs": {}}


def _save_state(subs_state: dict) -> None:
    try:
        atomic_write_text(_cache_path(), json.dumps({"subs": subs_state}))
    except OSError as e:  # persistence best-effort — never break the cycle
        logger.debug("reddit_ai cache save failed (non-fatal): %s", e)


def _fetch_sub(sub: str, state: dict) -> "tuple[str, list[dict] | None]":
    """One subreddit's /hot via Reddit's public JSON listing, through
    safe_http. Returns (status, posts) — posts is None unless status is
    "ok"; the caller decides what to reuse for every other status."""
    status, text = safe_http.fetch_text(
        _FEED_URL.format(sub=sub), state=state,
        headers={"User-Agent": _USER_AGENT},
        timeout=_TIMEOUT_SEC, name=f"reddit_ai:r/{sub}",
    )
    if status != "ok":
        return status, None
    if not text:
        # A 200 with an empty body is a failed fetch, not a quiet subreddit.
        logger.warning("reddit_ai: r/%s returned an empty body", sub)
        return "failed", None
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        # A parse failure is a FAILED fetch, not an empty feed -- returning
        # [] here would let the caller persist "this sub has nothing", which
        # is how a reshaped feed goes quiet instead of loud.
        logger.warning("reddit_ai: r/%s feed did not parse (%s)", sub, exc)
        return "failed", None
    if root.tag != f"{_ATOM_NS}feed":
        # Well-formed XML that is not an Atom feed -- a Cloudflare
        # interstitial, a login wall, an error page served as 200. It PARSES,
        # so a naive reader finds zero entries and reports an empty sub, and
        # the caller then persists "nothing here" and goes quiet. A wrong
        # document is a failed fetch, not a quiet feed (same class as the
        # ai_labs sticky-empty-feed bug).
        logger.warning("reddit_ai: r/%s returned <%s>, not an Atom feed",
                       sub, root.tag)
        return "failed", None
    out: list[dict] = []
    for entry in root.findall(f"{_ATOM_NS}entry"):
        title = _clean_title(entry.findtext(f"{_ATOM_NS}title") or "")
        if not title:
            continue
        link_el = entry.find(f"{_ATOM_NS}link")
        link = link_el.get("href") if link_el is not None else ""
        ident = (entry.findtext(f"{_ATOM_NS}id") or "").strip()
        out.append({"sub": sub, "title": title, "link": link, "id": ident})
        if len(out) >= 4:  # top 4 per sub
            break
    return status, out


def _fetch_all(subs_state: dict, now: float) -> list[dict]:
    # Reddit's anon quota is ~1 req/IP per ~30s window, so back-to-back
    # fetches meant the last sub in the list 429'd on every poll. Rotate
    # which sub goes first (hour-based, deterministic) so no sub is
    # permanently starved, and only sleep between subs THIS SWEEP actually
    # has to fetch — a sub still inside its own per-sub window costs nothing.
    order = list(_SUBS)
    shift = int(now // 3600) % len(order)
    order = order[shift:] + order[:shift]
    posts: list[dict] = []
    fetched_any = False
    for sub in order:
        entry = subs_state.setdefault(sub, {})
        last = safe_http.num(entry.get("last_fetched"))
        if now - last < _CACHE_TTL_SEC:
            # Already attempted (success OR failure) within this window --
            # the dark-feed fix: reuse whatever we have instead of
            # re-hitting reddit and re-paying the stagger sleep for a source
            # that is not due yet.
            posts.extend(entry.get("posts") or [])
            continue
        if fetched_any:
            time.sleep(30)
        fetched_any = True
        status, fetched = _fetch_sub(sub, entry)
        if status == "ok" and fetched is not None:
            entry["posts"] = fetched
        # not_modified/rate_limited/failed: keep whatever this sub already
        # had (possibly []) rather than wiping a known-good set on a
        # transient miss.
        posts.extend(entry.get("posts") or [])
        # Record the attempt regardless of outcome, so a persistently
        # failing sub is not retried — and slept for — again until the
        # window passes.
        entry["last_fetched"] = now
    return posts


def scan_reddit_ai() -> list[dict]:
    """Return triggers for hot AI/ML reddit posts."""
    state = _load_state()
    now = time.time()
    if "_legacy_posts" in state:
        posts = state["_legacy_posts"]
    else:
        subs_state = state.get("subs", {})
        posts = _fetch_all(subs_state, now)
        # Written even when `posts` ends up empty -- an all-dark sweep must
        # still record that every sub was just tried, or the next cycle
        # reads "no attempt on file" and re-pays the whole sleep budget.
        _save_state(subs_state)

    # A row with no title cannot make a meaningful trigger — letting one
    # through used to reach the workspace as `detail='r/?: ?'`, burning a
    # habituation slot on pure garbage.
    posts = [p for p in posts if isinstance(p, dict) and (p.get("title") or "").strip()]

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
        write_seen(_seen_path(), {_post_key(p): now for p in posts}, _SEEN_MAX)
        return []

    fresh = [p for p in posts if _post_key(p) not in seen]

    triggers: list[dict] = []
    for p in fresh[:6]:  # cap at 6 across subs
        slug = (p.get("id") or p.get("link") or "")[-40:]
        title = _clean_title(p.get("title") or "")
        brand_hit = bool(brand_re and brand_re.search(p.get("title", "") or ""))
        seen[_post_key(p)] = now
        triggers.append(
            {
                "type": "reddit_ai_post",
                "detail": (f"r/{p.get('sub','?')} BRAND MENTION: {title}"
                           if brand_hit else
                           f"r/{p.get('sub','?')}: {title}"),
                "novelty": safe_http.clamp01(0.9 if brand_hit else 0.75),
                "relevance": safe_http.clamp01(0.9 if brand_hit else 0.55),
                "urgency": safe_http.clamp01(0.75 if brand_hit else 0.25),
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
