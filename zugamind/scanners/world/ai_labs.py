"""AI lab research scanner — polls canonical AI lab feeds.

Direct source for cutting-edge research: Anthropic, OpenAI, DeepMind, Meta
FAIR, Google Research, Hugging Face Papers. Emits one trigger per fresh
post not seen in HABITUATION_HOURS. Surfaces as CIVILIZATION-domain
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

logger = logging.getLogger("zugamind.scanners.ai_labs")

_TIMEOUT = 8.0
_CACHE_TTL = 1800
_MAX_TRIGGERS = 4
# Honors ZUGAMIND_DATA_DIR without importing foundation — scanners stay standalone.
_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR") or Path(__file__).resolve().parent.parent.parent / "data")
_CACHE_DIR = _DATA_DIR / "scanner_cache"
_CACHE_FILE = _CACHE_DIR / "ai_labs.json"

_FEEDS = [
    ("anthropic",    "https://www.anthropic.com/news/rss.xml"),
    ("openai",       "https://openai.com/news/rss.xml"),
    ("deepmind",     "https://deepmind.google/discover/blog/rss.xml"),
    ("meta_fair",    "https://ai.meta.com/blog/rss/"),
    ("google_res",   "https://research.google/blog/rss/"),
    ("hf_papers",    "https://huggingface.co/papers/rss"),
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
                    summary = re.sub(r"<[^>]+>", "", txt)[:300]
            if title:
                items.append({"lab": lab, "title": title[:200], "link": link, "summary": summary})
        if len(items) >= 8:
            break
    return items


def scan_ai_labs() -> list[dict[str, Any]]:
    cached = _read_cache()
    if cached and "items" in cached:
        items = cached["items"]
    else:
        items = []
        for lab, url in _FEEDS:
            txt = _fetch(url)
            if not txt:
                continue
            items.extend(_parse_feed(txt, lab))
        _write_cache({"ts": time.time(), "items": items})

    triggers: list[dict[str, Any]] = []
    for it in items[:_MAX_TRIGGERS]:
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
