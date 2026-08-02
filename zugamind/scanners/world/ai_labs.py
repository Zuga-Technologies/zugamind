"""AI lab research scanner — polls canonical AI lab feeds.

Direct source for cutting-edge research: Anthropic, OpenAI, DeepMind,
Google Research, Hugging Face Papers, Microsoft Research. Emits one trigger
per fresh post not seen in HABITUATION_HOURS. Surfaces as CIVILIZATION-domain
research input for the self-modification bridge.

Stdlib only. Failure-silent per scanner contract. Cached 30min on disk.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from foundation.text_format import truncate_title

logger = logging.getLogger("zugamind.scanners.ai_labs")

_TIMEOUT = 8.0
_CACHE_TTL = 1800
_MAX_TRIGGERS = 4
# Honors ZUGAMIND_DATA_DIR without importing foundation — scanners stay standalone.
_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR") or Path(__file__).resolve().parent.parent.parent / "data")
_CACHE_DIR = _DATA_DIR / "scanner_cache"
_CACHE_FILE = _CACHE_DIR / "ai_labs.json"

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
        if not _CACHE_FILE.exists():
            return None
        d = json.loads(_CACHE_FILE.read_text())
        if time.time() - d.get("ts", 0) > _CACHE_TTL:
            return None
        return d
    except Exception:
        return None


def _write_cache(payload: dict[str, Any]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(payload))
    except Exception:
        pass


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
            if title:
                items.append({"lab": lab, "title": truncate_title(title, 200), "link": link, "summary": summary})
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
                      "link": "https://www.anthropic.com" + href, "summary": ""})
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
        items.append({"lab": "hf_papers", "title": title[:200],
                      "link": f"https://huggingface.co/papers/{pid}" if pid else "",
                      "summary": (paper.get("summary") or "")[:300]})
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

    triggers: list[dict[str, Any]] = []
    for it in _round_robin(items)[:_MAX_TRIGGERS]:
        detail = f"[{it['lab']}] {it['title']}"
        if it.get("summary"):
            detail += " -- " + it["summary"][:160]
        triggers.append({
            "type": "ai_lab_research",
            "detail": detail[:380],
            "lab": it["lab"],
            "title": it["title"],
            "link": it.get("link", ""),
            "summary": it.get("summary", ""),
            "novelty": 0.8,
            "relevance": 0.75,
            "urgency": 0.25,
        })
    return triggers
