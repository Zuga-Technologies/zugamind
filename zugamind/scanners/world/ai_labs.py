"""AI lab research scanner — polls canonical AI lab feeds.

Direct source for cutting-edge research: Anthropic, OpenAI, DeepMind,
Google Research, Hugging Face Papers, Microsoft Research. Emits one trigger
per post it has never emitted before. Surfaces as CIVILIZATION-domain
research input for the self-modification bridge.

Two things here are load-bearing and were both missing until 2026-08-18,
when a Jul-24 post ("Introducing Claude Opus 5") won the global workspace
eight separate times over twelve days and bought a harness wake:

1. DEDUPE IS PER-ITEM AND PERSISTENT (`ai_labs_seen.json`), the same "seen
   link" shape the sibling example scanners use. The 30-minute `_CACHE_TTL`
   is a FETCH cache, not a dedupe — it only limits how often we hit the
   network. Engine-level habituation (`HABITUATION_HOURS`, default 6) is a
   rate limiter, not a dedupe either: without a seen-set, a post that stays
   at the top of a feed re-fires every 6 hours forever. On a cold start the
   seen-set is baselined SILENTLY (whole feed marked seen, zero triggers) so
   deploying this never dumps a month of back-catalogue into the workspace.

2. RELEVANCE IS THE POST'S SUBJECT, NOT A CONSTANT, AND IT NEEDS RANGE.
   WorldSignals prices a trigger at 0.25 + 0.4*relevance + 0.2*urgency, so a
   hardcoded relevance=0.75 put EVERY fresh lab post at exactly 0.600 -- the
   wake floor, cleared by a margin of zero. That is how a government-policy
   announcement bought a Claude session on 2026-08-18 20:11Z: the topic
   never entered the math.

   The first fix that morning added ONE demotion tier, which took the
   subject from 1 value to 2 -- still near-constant. It held for 3h25m: at
   23:48Z an OpenAI customer case study ("How NVIDIA scales expertise with
   ChatGPT Work") bought a session on the identical 0.600, because it is
   marketing rather than public affairs and the word list only knew the
   latter. A word list stops the shape it was written for; only range stops
   the class. So relevance is three tiers, and the classes are ordered by
   what they DO to a builder:

     HIGH (0.95, bids 0.68)  ships something to build on -- a model launch,
                             pricing, API/SDK, a deprecation.
     DEFAULT (0.75, bids 0.60)  unrecognized on a curated feed. Fails OPEN.
     FIREHOSE (0.40, bids 0.46)  unrecognized on a feed with a daily quota.
                             See 4 below.
     NON-WORK (0.40, bids 0.46)  the lab talking about itself -- public
                             affairs (policy, regulation, elections,
                             appointments, partnerships, economic-index
                             reports) AND promotion (customer case studies,
                             enterprise success stories). Real news, no
                             bearing on building on these APIs.

   NON-WORK is checked FIRST and wins over HIGH: a case study names the
   product it is selling ("...with GPT-5.6 Sol"), so letting a model token
   promote it would re-open the exact hole. Range is what makes the gate
   survive a floor that MOVES -- the wake floor self-calibrates
   (act/floor_calibration.py), and at 0.600 every non-demoted post cleared
   it by zero while three more such winners would have ratcheted it to 0.65
   and silenced the whole feed, model launches included. HIGH sits above
   that.

4. FAIL-OPEN IS A PROPERTY OF THE FEED, NOT A CONSTANT.
   DEFAULT was set to 0.600 and described as both "fails OPEN" and "sits
   below the floor". Those cannot both hold, because the floor MOVES: it is
   p90 of the last 50 ambient winners + 0.05, and it had settled at 0.590 by
   2026-08-19T02Z. So DEFAULT cleared it -- by 0.007 and by 0.008 -- and
   bought two consecutive Claude sessions 36 minutes apart, at 02:08Z and
   02:44Z, both for HuggingFace daily-paper abstracts (evolution strategies
   for LLM agents; 3D Gaussian Splatting generation). Neither ships anything.

   Worse, the same posts were what SET the 0.590. Over the 51 world_signals
   winners of 2026-08-18, ambient chatter bid 0.51-0.55 and the only samples
   above it were ai_labs DEFAULT items at 0.597-0.600. A high-volume feed
   priced at exactly the floor is a self-referential trap: its own bids
   become the p90, the p90 lifts the floor toward 0.65, and HIGH's 0.68 is
   left with 0.03 of headroom before model launches go silent too.

   The volume is what makes it a trap, and the volume is a property of the
   FEED. anthropic/openai/deepmind publish when something happened; hf_papers
   serves a fixed ten a day whether or not it did, so on that feed
   "unrecognized" is the base case rather than a near-miss, and fail-open
   pays wake price for the whole quota. `_FIREHOSE_LABS` prices unrecognized
   items from those feeds at NON-WORK level. They still enter the workspace
   as research input -- bidding into the workspace is not the same as buying
   a session. Fail-open is kept intact on the six curated feeds, where the
   whole record shows no DEFAULT-tier wake that was not a subject the word
   lists have since learned.

   NOT fixed by this, and worth knowing: the floor still chases whatever the
   loudest routine source is, and HIGH is a fixed 0.68. If ambient chatter
   ever drifts above 0.63, model launches go deaf and no change in this file
   can stop it.

5. A LAB APPLYING AI TO ANOTHER SCIENTIFIC FIELD IS ALSO NOT BUILDER NEWS.
   A post about weather forecasting, quantum chemistry, protein folding, or
   drug discovery is real research from the lab itself, so it is neither
   public affairs nor promotion -- the existing NON-WORK checks miss it --
   and it carries no HIGH-tier language (no "introducing", no model
   name+version, no API/pricing), so it lands on DEFAULT and buys a full
   wake same as any model launch. This is not a one-off: [deepmind]
   "WeatherNext: AI model achieves breakthrough in forecasting cyclones"
   won the workspace three times in 30h (2026-08-06 to -08), and
   [msft_research] "Broadening access to Skala creates a faster path to
   predictive DFT" won again on 2026-08-20 -- a brand-new post, so the
   dedupe fix in (1) does not touch this, only a subject check does.
   Demoted the same way as public affairs/promo: checked FIRST, before
   HIGH, on title+summary.

3. URGENCY DECAYS WITH PUBLISH AGE. Feed order is not recency order —
   anthropic.com/news leads with a pinned/featured block, so its FIRST card
   was a month old while the newest post sat fifth and was never reached.
   A trigger's numbers have to be evidence: a constant urgency means the bid
   is identical whether the item is an hour old or a year old. Age unknown
   (unparseable date) fails OPEN at the fresh value — a parser regression
   must not silently make the scanner deaf.

6. THE PUBLIC-AFFAIRS LIST ITSELF STILL GOES STALE, SAME AS ANY WORD LIST.
   [openai] "Introducing AI Futures" won the workspace at HIGH on 2026-08-20
   20:13Z: the title's "Introducing" satisfies _HIGH_RELEVANCE_RE, and the
   post is a societal-impact essay ("...reshape power, governance, the
   economy, and individual freedom") -- public affairs by kind, same as the
   2026-08-18 "Strengthening Democratic Oversight in National Security"
   post that started this tier -- but it uses none of that post's words.
   Not fixed by adding "governance": the comment above _PUBLIC_AFFAIRS_RE
   already flags "governance" as a term with a technical reading (see
   test_work_posts_keep_the_operational_relevance's "AI governance for
   model deployment", which must stay DEFAULT), so a bare match there would
   re-open the hole point 2's "policy" caveat was written to avoid. "power"
   is worse -- compute/processing "power" is routine builder vocabulary.
   "individual freedom" has no such reading and is the phrase this post
   actually turns on; added alone rather than paired with either broader
   word.

7. GEOSPATIAL/MOBILITY RESEARCH IS THE SAME CLASS AS 5, NOT A NEW ONE.
   [google_res] "How mobility gives language models a deeper understanding
   of place" won the workspace on DEFAULT at 0.66 on 2026-08-21: real Earth
   AI research (visit-intent prediction, price-level classification,
   busyness estimation from anonymized mobility data), zero shipping
   language, so same miss as WeatherNext/Skala -- "AI applied to a field"
   just has more fields than the four point 5 enumerated. Extended
   `_SCI_DOMAIN_RE` rather than adding a point 8 next time this class
   recurs with a still-different field; the pattern is the category, not
   each individual subject.

8. EVALUATION-GOVERNANCE WORK WITH OUTSIDE INSTITUTES IS PUBLIC AFFAIRS.
   [deepmind] "Piloting the world's first double-blind AI evaluations"
   bought a wake on DEFAULT at exactly 0.600 vs a 0.590 floor on
   2026-08-27 — the first curated-feed DEFAULT wake since point 4 was
   written. The post: a methodology pilot with the Singapore AI Safety
   Institute, OpenMined, AVERI and MLCommons — assurance infrastructure
   for evaluators and institutes, nothing that changes what a builder can
   build or what it costs. The deepmind feed's summary is the title
   verbatim, so the headline was the only evidence available. Two things
   deliberately NOT done: "piloting" was not added to _HIGH_RELEVANCE_RE
   (this post is the proof that a pilot is not a launch, even though it
   rhymes with "previewing"), and bare "evaluation" was not demoted (it
   has a technical reading — see test_work_posts_keep_the_operational_
   relevance's "cybersecurity evaluations" title, which must stay
   DEFAULT). "double-blind" is the phrase-level tell — clinical-trial
   vocabulary no shipping post uses — and "safety institute(s)" names the
   institutional class directly, for the sibling posts that will phrase
   the same news as a partnership.

Stdlib only. Failure-silent per scanner contract. Cached 30min on disk.
"""
from __future__ import annotations

import calendar
import email.utils
import json
import logging
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foundation.text_format import truncate_title
from scanners.seen_items import read_seen, write_seen

logger = logging.getLogger("zugamind.scanners.ai_labs")

_TIMEOUT = 8.0
_CACHE_TTL = 1800
_MAX_TRIGGERS = 4
_SEEN_MAX = 800     # seven feeds x eight items = 56 keys/sweep; this holds ~2 weeks
_FRESH_HOURS = 24   # published inside this window scores full urgency
_STALE_HOURS = 72   # published beyond this scores zero — backlog, not news
_RELEVANCE_HIGH = 0.95      # ships something to build on — bids 0.68 fresh, clears a moving floor
_RELEVANCE_DEFAULT = 0.75   # unrecognized subject on a curated feed — bids 0.60 fresh
_RELEVANCE_FIREHOSE = 0.40  # unrecognized subject on a firehose feed — bids 0.46 fresh, under the floor
_RELEVANCE_NON_WORK = 0.40  # the lab talking about itself — bids 0.46 fresh, under the floor
# Feeds that publish a fixed quota per day regardless of whether anything
# happened. On these, "unrecognized" is the BASE CASE, not a near-miss, so
# fail-open prices the whole quota at wake level — see the FIREHOSE section of
# the module docstring. FIREHOSE and NON_WORK price the same today for
# different reasons; they are separate constants so either can move alone.
_FIREHOSE_LABS = frozenset({"hf_papers"})
# Honors ZUGAMIND_DATA_DIR without importing foundation — scanners stay standalone.
_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR") or Path(__file__).resolve().parent.parent.parent / "data")
_CACHE_DIR = _DATA_DIR / "scanner_cache"


# Resolved per call, not bound at import: tests isolate this scanner by
# patching _CACHE_DIR alone (tests/conftest.py), and a module-level
# `_CACHE_FILE = _CACHE_DIR / ...` would keep pointing at the real data
# directory after that patch — which is exactly how the pre-2026-08-18
# constant let the suite read and overwrite the live cache.
def _cache_file() -> Path:
    return _CACHE_DIR / "ai_labs.json"


def _seen_file() -> Path:
    return _CACHE_DIR / "ai_labs_seen.json"

# Feed audit: anthropic never had an RSS feed (404 since introduction; the
# /news and /research indexes ARE server-rendered, so scrape them), deepmind
# moved off /discover, meta_fair has no feed at all (removed), hf_papers RSS
# is auth-walled but the JSON API is open. Third field picks the parser.
_FEEDS = [
    ("anthropic",      "https://www.anthropic.com/news",                   "anthropic_html"),
    ("anthropic_res",  "https://www.anthropic.com/research",               "anthropic_html"),
    ("openai",         "https://openai.com/news/rss.xml",                  "rss"),
    ("deepmind",       "https://deepmind.google/blog/rss.xml",             "rss"),
    ("google_res",     "https://research.google/blog/rss/",                "rss"),
    ("hf_papers",      "https://huggingface.co/api/daily_papers?limit=10", "hf_json"),
    ("msft_research",  "https://www.microsoft.com/en-us/research/feed/",   "rss"),
]

_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _read_cache() -> dict[str, Any] | None:
    try:
        path = _cache_file()
        if not path.exists():
            return None
        d = json.loads(path.read_text())
        if time.time() - d.get("ts", 0) > _CACHE_TTL:
            return None
        return d
    except Exception:
        return None


def _write_cache(payload: dict[str, Any]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_file().write_text(json.dumps(payload))
    except Exception:
        pass


def _read_seen() -> dict[str, float] | None:
    """None means COLD START — see scanners.seen_items.read_seen."""
    return read_seen(_seen_file())


def _write_seen(seen: dict[str, float]) -> None:
    write_seen(_seen_file(), seen, _SEEN_MAX)


def _item_key(it: dict[str, Any]) -> str:
    """Stable identity for one feed item. The permalink where a feed gives
    one; lab+title otherwise (hf_papers without an id, a malformed entry)."""
    link = (it.get("link") or "").strip()
    return link or f"{it.get('lab', '?')}|{it.get('title', '')}"


_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
# "Product Jul 24, 2026 Introducing ..." / "Aug 14, 2026 Announcements ..." —
# anthropic.com renders the date inside the card, in either position.
_TEXT_DATE_RE = re.compile(r"\b([A-Z][a-z]{2})[a-z]*\s+(\d{1,2}),\s+(\d{4})\b")


def _parse_date(value: str) -> float | None:
    """Epoch seconds from an RFC-822 (RSS), ISO-8601 (Atom/JSON), or
    "Mon D, YYYY" (scraped card) date. None when nothing parses."""
    value = (value or "").strip()
    if not value:
        return None
    try:  # RFC 822 — "Tue, 14 Aug 2026 09:00:00 +0000"
        dt = email.utils.parsedate_to_datetime(value)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    except Exception:
        pass
    try:  # ISO 8601 — "2026-08-14T09:00:00Z"
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        pass
    m = _TEXT_DATE_RE.search(value)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            try:
                return datetime(int(m.group(3)), month, int(m.group(2)),
                                tzinfo=timezone.utc).timestamp()
            except ValueError:
                return None
    return None


# A frontier lab publishes three kinds of thing, and they are worth
# different amounts to someone BUILDING on the APIs.
#
# NON-WORK, tier one: public affairs — policy positions, regulatory comment,
# government relations, executive appointments, org partnerships,
# economic-impact reports. Real news that changes nothing about the building.
# Only phrases unambiguous in that sense belong here; a term with a technical
# reading ("governance", "alignment policy", an RL "policy") would make the
# scanner deaf to actual work. Checked against a 56-item live sweep (7 feeds,
# 2026-08-18): 5 matched on the original list, all public affairs, no research
# or product post among them. The appointment/partnership/government-relations
# phrases were added 2026-08-19 — the module docstring already CLAIMED to cover
# "executive appointments", but no pattern did, and "OpenAI appoints Dali Rajic
# as Chief Revenue Officer" won the workspace at full relevance on 2026-08-13.
# "individual freedom" was added 2026-08-20 for a societal-impact essay that
# used none of the words above — see point 6 in the module docstring for why
# it is not "governance" or "power" instead. "double-blind" and "safety
# institute(s)" were added 2026-08-27 for an evaluation-governance pilot with
# national AI safety institutes — see point 8 for why the match is these two
# phrases and not "evaluation" (which has a technical reading the work-posts
# test protects) or "piloting" promoted to HIGH (a pilot is not a launch).
_PUBLIC_AFFAIRS_RE = re.compile(
    r"national\s+security|democratic\s+oversight|public\s+policy"
    r"|\bai\s+policy\b|policy\s+ideas|policymaker|regulator|regulation"
    r"|legislation|lawmaker|\bcongress\b|\bparliament\b|\bai\s+act\b"
    r"|\belections?\b|\bgovernments?\b|global\s+affairs|public\s+affairs"
    r"|philanthrop|\bnonprofit\b|economic\s+index|economic\s+research"
    r"|\bappoints?\b|\bappointment\b|chief\s+\w+\s+officer"
    r"|to\s+join\s+\w+\s+as\b|\bboard\s+of\s+directors\b|\bgovernor\b"
    r"|\bpartnering\s+with\b|\bpartnership\b|\bletter\s+to\b|\bstatement\s+on\b"
    r"|\b(?:joins|joining)\s+(?:the\s+)?[\w-]+\s+"
    r"(?:project|initiative|coalition|alliance|consortium|council|pledge)\b"
    r"|\bindividual\s+freedom\b|\bdouble-blind\b|\bsafety\s+institutes?\b",
    re.IGNORECASE,
)

# NON-WORK, tier two: promotion — a customer case study is the lab selling its
# product with a named buyer as the proof. It reads technical (it names models,
# quotes engineering outcomes) and is worth nothing to a builder: you cannot act
# on the news that Asana was happy. Two headline grammars carry most of it, and
# both are matched CASE-SENSITIVELY on the buyer's proper noun — that capital
# letter is the whole discriminator. Without it "How Claude's text watermark
# works" (technical) reads the same as "How NVIDIA scales expertise with
# ChatGPT Work" (an ad).
_PROMO_BUYER = r"[A-Z][\w.&'’-]*(?:\s+[A-Z][\w.&'’-]*){0,3}"
_PROMO_OUTCOME = (
    r"uses?|used|is\s+using|scales?|scaled|builds?|built|cuts?|cut|saves?|saved"
    r"|reduces?|reduced|automat\w+|transform\w+|accelerat\w+|deploys?|deployed"
    r"|puts?|put|completes?|completed|clears?|cleared|ships?|shipped|grew|grows?"
)
# "How NVIDIA scales expertise with ChatGPT Work"
_PROMO_HOW_RE = re.compile(rf"\bHow\s+{_PROMO_BUYER}\s+(?:{_PROMO_OUTCOME})\b")
# "Asana cleared 5 years of engineering work in 2 weeks with Codex"
_PROMO_OUTCOME_RE = re.compile(
    rf"^{_PROMO_BUYER}\s+(?:{_PROMO_OUTCOME})\b.*\bwith\s+[A-Z]"
)
# A third grammar needs no buyer capture at all: "powered by" is a partner
# describing whose engine runs under their own product, and no lab post about
# its OWN model is ever phrased that way about itself. Caught the 2026-08-19
# 14:29Z wake — "Replit expands access to software creation with GPT-5.6
# Luna" / "Replit introduces Free Mode, powered by GPT-5.6 Luna" — neither
# _PROMO_HOW_RE ("expands" opens the title, not "How") nor _PROMO_OUTCOME_RE
# ("expands" and "introduces" are not outcome verbs) matched, so the model
# token alone bought it HIGH.
_PROMO_KEYWORD_RE = re.compile(
    r"\bcase\s+stud|customer\s+stor|\bsuccess\s+stor|\btestimonial"
    r"|\bhow\s+enterprises\b|\bpowered\s+by\b",
    re.IGNORECASE,
)

# NON-WORK, tier three: AI applied to a different scientific field. Real lab
# research, but nothing here bears on building with the models -- see points
# 5 and 7 in the module docstring for the evidence (WeatherNext x3, Skala/DFT,
# mobility/POI understanding). Kept to unambiguous phrases from confirmed
# hits plus the same flagship "AI for science" categories (protein folding,
# drug discovery, materials discovery), not a bare acronym like "DFT" alone,
# which also means Discrete Fourier Transform and could false-positive on a
# real architecture post. Same reasoning for "mobility" -- not matched bare,
# since robotics posts use it for actuation/movement, not geospatial data --
# only paired with the geospatial-data phrasing this class of post actually
# uses.
_SCI_DOMAIN_RE = re.compile(
    r"weather\s+forecast|forecast\w*\s+(?:weather|cyclones?|hurricanes?|storms?)"
    r"|(?:cyclones?|hurricanes?|storms?)\s+forecast|climate\s+model"
    r"|exchange-correlation\s+functional|density\s+functional\s+theory|predictive\s+dft"
    r"|quantum\s+chemistry|molecular\s+dynamics"
    r"|protein\s+fold|protein\s+structure\s+predict"
    r"|drug\s+discovery|drug\s+design"
    r"|materials?\s+discovery"
    r"|mobility\s+(?:data|patterns?)|understanding\s+of\s+place",
    re.IGNORECASE,
)

# HIGH: the post changes what can be built or what it costs. Deliberately NOT
# "is this research" — hf_papers alone publishes ten papers a day, and promoting
# that firehose would spend a session on each. Shipping changes only.
#
# Matched against the TITLE ALONE, unlike the demotion patterns above. Every
# paper abstract benchmarks against frontier models, so reading summaries here
# promotes the whole firehose on a passing mention: measured on the live sweep,
# a document-extraction paper cleared HIGH purely because its abstract contained
# "sonnet-5". Demotion reads the summary because openai's RSS carries the
# subject in the description and a wrong demotion only costs silence; promotion
# does not, because a wrong promotion spends a Claude session.
_HIGH_RELEVANCE_RE = re.compile(
    r"\bintroducing\b|\bannouncing\b|\bpreviewing\b|\blaunching\b"
    r"|\bnow\s+available\b|\bgenerally\s+available\b|\bdeprecat\w+"
    r"|\bsunsett?ing\b|\bbreaking\s+change|\bmigration\s+guide\b"
    r"|\bpricing\b|\brate\s+limits?\b|\brelease\s+notes\b|\bchangelog\b"
    r"|\bapis?\b|\bsdks?\b"
    r"|\b(?:gpt|claude|gemini|llama|qwen|opus|sonnet|haiku|fable|codex)"
    r"[\s‑-]?\d",
    re.IGNORECASE,
)


def _is_non_work(text: str) -> bool:
    """The lab talking about itself, selling itself, or doing science that
    isn't about the models — public affairs, promotion, or an off-topic
    scientific domain."""
    return bool(
        _PUBLIC_AFFAIRS_RE.search(text)
        or _PROMO_HOW_RE.search(text)
        or _PROMO_OUTCOME_RE.search(text)
        or _PROMO_KEYWORD_RE.search(text)
        or _SCI_DOMAIN_RE.search(text)
    )


def _relevance_for(title: str, summary: str = "", lab: str = "") -> float:
    """How much this post bears on building with these models.

    Order is load-bearing. NON-WORK is asked FIRST because promotional copy
    quotes the product it sells, so a model token inside a case study must not
    buy it the HIGH tier. NON-WORK reads title AND summary; HIGH reads the
    title only — see the comment above _HIGH_RELEVANCE_RE for why the two
    directions do not get the same evidence.

    HIGH is asked BEFORE the firehose demotion, so a paper feed that does
    announce a shipped thing still promotes — the demotion prices the quota,
    it does not blind the feed.

    An unrecognized subject fails OPEN at the operational default, so a
    vocabulary that drifts out of date makes the mind no deafer than it
    already is — but only on a feed where "unrecognized" is a near-miss. On a
    firehose it is the base case, and fail-open there means paying for the
    quota. `lab` defaults to "" (curated) so an unlabelled call keeps the old
    answer.
    """
    if _is_non_work(f"{title or ''} {summary or ''}"):
        return _RELEVANCE_NON_WORK
    if _HIGH_RELEVANCE_RE.search(title or ''):
        return _RELEVANCE_HIGH
    if lab in _FIREHOSE_LABS:
        return _RELEVANCE_FIREHOSE
    return _RELEVANCE_DEFAULT


def _urgency_for(published: float | None, now: float) -> float:
    """Urgency is publish age, not a constant. Unknown age fails OPEN at the
    fresh value: a feed that stops exposing dates must not go silent."""
    if published is None:
        return 0.25
    age_h = max(0.0, (now - published) / 3600.0)
    if age_h <= _FRESH_HOURS:
        return 0.25
    if age_h >= _STALE_HOURS:
        return 0.0
    span = _STALE_HOURS - _FRESH_HOURS
    return round(0.25 * (1.0 - (age_h - _FRESH_HOURS) / span), 4)


def _fetch(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ZugaMind/scanner"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        logger.debug("ai_labs fetch %s failed: %s", url, e)
        return None


def _parse_feed(xml_text: str, lab: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return items
    for it in root.iter():
        tag = it.tag.lower().split("}")[-1]
        if tag in ("item", "entry"):
            title = ""
            link = ""
            summary = ""
            published = None
            for child in it:
                ctag = child.tag.lower().split("}")[-1]
                txt = (child.text or "").strip()
                if ctag == "title":
                    title = txt
                elif ctag == "link":
                    href = child.attrib.get("href")
                    link = href if href else txt
                elif ctag in ("description", "summary"):
                    summary = truncate_title(re.sub(r"<[^>]+>", "", txt), 300)
                elif ctag in ("pubdate", "published", "updated", "date") and published is None:
                    published = _parse_date(txt)
            if title:
                items.append({"lab": lab, "title": truncate_title(title, 200), "link": link,
                              "summary": summary, "published": published})
        if len(items) >= 8:
            break
    return items


def _parse_anthropic_html(html: str, lab: str) -> list[dict[str, str]]:
    """Scrape a server-rendered anthropic.com index page (no RSS exists).

    Covers both /news/<slug> and /research/<slug> cards — research posts
    do not appear on /news.
    """
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a[^>]+href="(/(?:news|research)/[a-z0-9-]+)"[^>]*>(.*?)</a>', html, re.S):
        href, inner = m.group(1), m.group(2)
        if href in seen:
            continue
        seen.add(href)
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inner)).strip()
        if len(title) < 8:  # skip bare nav anchors
            continue
        items.append({"lab": lab, "title": title[:200],
                      "link": "https://www.anthropic.com" + href, "summary": "",
                      "published": _parse_date(title)})
        if len(items) >= 8:
            break
    return items


def _parse_hf_json(txt: str) -> list[dict[str, str]]:
    """Parse the open huggingface.co/api/daily_papers JSON (RSS is auth-walled)."""
    items: list[dict[str, str]] = []
    try:
        data = json.loads(txt)
    except Exception:
        return items
    for p in data[:8] if isinstance(data, list) else []:
        if not isinstance(p, dict):
            continue
        paper = p.get("paper") if isinstance(p.get("paper"), dict) else p
        title = (paper.get("title") or "").strip().replace("\n", " ")
        if not title:
            continue
        pid = paper.get("id") or p.get("id") or ""
        published = _parse_date(str(paper.get("publishedAt") or p.get("publishedAt") or ""))
        items.append({"lab": "hf_papers", "title": title[:200],
                      "link": f"https://huggingface.co/papers/{pid}" if pid else "",
                      "summary": (paper.get("summary") or "")[:300],
                      "published": published})
    return items


def _round_robin(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Interleave items per lab so one feed can't monopolize _MAX_TRIGGERS."""
    by_lab: dict[str, list] = {}
    for it in items:
        by_lab.setdefault(it.get("lab", "?"), []).append(it)
    ordered: list[dict[str, str]] = []
    while any(by_lab.values()):
        for lab_items in by_lab.values():
            if lab_items:
                ordered.append(lab_items.pop(0))
    return ordered


def scan_ai_labs() -> list[dict[str, Any]]:
    cached = _read_cache()
    if cached and "items" in cached:
        items = cached["items"]
    else:
        items = []
        for lab, url, fmt in _FEEDS:
            txt = _fetch(url)
            if not txt:
                continue
            if fmt == "anthropic_html":
                items.extend(_parse_anthropic_html(txt, lab))
            elif fmt == "hf_json":
                items.extend(_parse_hf_json(txt))
            else:
                items.extend(_parse_feed(txt, lab))
        _write_cache({"ts": time.time(), "items": items})

    if not items:
        return []  # every feed down: stay cold rather than baseline an empty sweep

    seen = _read_seen()
    if seen is None:
        # Cold start: baseline the whole sweep and say nothing. Without this,
        # deploying the seen-set would read a month of back-catalogue as
        # breaking news and burst _MAX_TRIGGERS of it into the workspace.
        # Cost: a post published inside this very sweep is missed once.
        _write_seen({_item_key(it): time.time() for it in items})
        return []

    now = time.time()
    unseen = [it for it in items if _item_key(it) not in seen]
    # Newest first WITHIN each lab before round-robin: feed order is publisher
    # order, and a pinned/featured block puts month-old posts at the top.
    unseen.sort(key=lambda it: it.get("published") or 0.0, reverse=True)

    triggers: list[dict[str, Any]] = []
    for it in _round_robin(unseen)[:_MAX_TRIGGERS]:
        detail = f"[{it['lab']}] {it['title']}"
        if it.get("summary"):
            detail += " -- " + it["summary"][:160]
        published = it.get("published")
        seen[_item_key(it)] = now
        triggers.append({
            "type": "ai_lab_research",
            "detail": detail[:380],
            "lab": it["lab"],
            "title": it["title"],
            "link": it.get("link", ""),
            "summary": it.get("summary", ""),
            "published": published,
            "novelty": 0.8,
            "relevance": _relevance_for(it["title"], it.get("summary", ""), it["lab"]),
            "urgency": _urgency_for(published, now),
        })

    if triggers:
        _write_seen(seen)
    return triggers
