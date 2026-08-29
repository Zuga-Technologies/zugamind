"""Shared HTTP + value hygiene for scanners.

Every scanner in this package reaches the open internet and hands what it
finds to an autonomous agent that can wake a human and spend money. Three
things kept being re-implemented per file, each slightly differently, and the
differences were where the bugs lived (audit 2026-08-29):

  opener()      an opener that does NOT hand your credentials to whatever
                host answers a redirect
  decode_body() one decoding rule, tolerant of a BOM and of one bad byte
  clamp01/num() one coercion rule for the 0..1 score contract

Stdlib only.
"""
from __future__ import annotations

import gzip  # noqa: F401  (kept: callers may hand us pre-decompressed bodies)
import json
import logging
import os
import zlib
import math
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

logger = logging.getLogger("zugamind.scanners.http")

# Headers that must never survive a hop to a different origin.
_CREDENTIAL_HEADERS = ("authorization", "proxy-authorization", "cookie",
                       "www-authenticate", "x-api-key")


class _CredentialStrippingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Drop credential headers when a redirect leaves the original origin.

    urllib's stock handler copies EVERY header except content-length and
    content-type onto the redirect target, with no host check at all:

        newheaders = {k: v for k, v in req.headers.items()
                      if k.lower() not in CONTENT_HEADERS}

    So a `Authorization: Bearer <token>` added for api.github.com is handed to
    whatever host answers a 30x — cross-origin, and in cleartext if the
    redirect downgrades to http. GitHub is not expected to redirect off-host
    today; the defect is that nothing here would stop it if it did (a
    compromised CDN, a corporate MITM proxy, an HTTPS_PROXY, a DNS hijack).
    A credential-handling defect with a one-line fix does not get to wait for
    a proof of exploitation.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        src, dst = urlsplit(req.full_url), urlsplit(newurl)
        same_origin = (src.scheme == dst.scheme and src.netloc == dst.netloc)
        if same_origin:
            return new
        stripped = [h for h in list(new.headers)
                    if h.lower() in _CREDENTIAL_HEADERS]
        for header in stripped:
            del new.headers[header]
        if stripped:
            # Names only. The value is the thing we are protecting.
            logger.warning(
                "scanners: redirect %s -> %s leaves the origin; stripped %s",
                src.netloc or "?", dst.netloc or "?", ", ".join(sorted(stripped)),
            )
        return new


def opener() -> urllib.request.OpenerDirector:
    """An opener that will not leak credentials across an origin boundary."""
    return urllib.request.build_opener(_CredentialStrippingRedirectHandler)


def decode_body(raw: bytes) -> str:
    """Bytes -> text, tolerantly.

    `utf-8-sig` because a CDN or proxy can prepend a BOM, which plain utf-8
    decodes into a leading \\ufeff that makes json.loads fail with a message
    naming the fix nobody reads. `errors="replace"` because one bad byte
    should cost one character, not the entire response — which is what a bare
    .decode("utf-8") did in hackernews.py.
    """
    return (raw or b"").decode("utf-8-sig", errors="replace")


def num(value, default: float = 0.0) -> float:
    """Coerce to a finite float, or the default. Never raises.

    Feed payloads are third-party JSON: a score can arrive as a string, null,
    or NaN. Arithmetic on any of those either raises mid-sweep (killing every
    remaining item) or produces a NaN that silently wins every comparison.
    """
    if isinstance(value, bool):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def clamp01(value, default: float = 0.0) -> float:
    """The 0..1 score contract, enforced at the emit site.

    novelty/relevance/urgency are documented 0..1 and are the currency of the
    workspace auction — an out-of-range score does not just look wrong, it
    outbids every honest sense. hackernews could emit -2499.7 from a negative
    score, and NaN silently read as the maximum.
    """
    return max(0.0, min(1.0, num(value, default)))

# ---------------------------------------------------------------------------
# The shared fetch. Four scanners were doing this four different ways.
# ---------------------------------------------------------------------------
#
# Measured across the four pollers on 2026-08-29: four timeouts (6.0 / 5 /
# 8.0 / 8.0) with no stated reason, three decoding rules, four error-handling
# shapes, zero of them handling 429, and zero doing conditional GET -- which
# matters most on GitHub, where a 304 does not count against the rate limit at
# all. Worse, a rate-limited source is indistinguishable from a quiet one, so
# the scheduler's yield backoff then LENGTHENS its cadence toward the 6h
# ceiling, punishing a source for being throttled.

_DEFAULT_TIMEOUT = 8.0
# Log at debug for a blip, at warning once it is a pattern. Every failure path
# in all four pollers logged at debug only, so a month-dark source left no
# line an operator could find at the default level.
_FAILS_BEFORE_LOUD = 3
# Hard ceiling on a response body. Two reasons, and the second is the sharp one:
# a feed that suddenly returns 200 MB should cost us one skipped cycle, not the
# process; and the XML parser these bodies feed (xml.etree.ElementTree) is
# vulnerable to entity-expansion blowup ("billion laughs"). ElementTree does
# NOT resolve external entities, so XXE is not the exposure here -- expansion
# is, and expansion needs a body to expand from. defusedxml is the usual answer
# and is a third-party dependency, which this package cannot take (stdlib-only
# is a hard rule), so the bound goes here, at the only place bytes enter.
_MAX_BODY_BYTES = int(os.environ.get("ZUGAMIND_MAX_FEED_BYTES", str(8 * 1024 * 1024)))


def fetch_text(url: str, *, state: dict, headers: "dict | None" = None,
               timeout: float = _DEFAULT_TIMEOUT, name: str = "") -> tuple:
    """Same contract as fetch_json, but returns the decoded BODY TEXT.

    Not every feed worth polling is JSON. Without this, a caller that wanted
    the conditional-GET and rate-limit handling had only one way to get it --
    change what it fetches -- and on 2026-08-29 that is exactly what happened
    to reddit_ai, whose .json alternative turns out to be 403 Blocked while
    its .rss feed answers fine. The helper, not the caller, was the thing
    missing.
    """
    return _fetch(url, state=state, headers=headers, timeout=timeout,
                  name=name, parse=False)


def fetch_json(url: str, *, state: dict, headers: "dict | None" = None,
               timeout: float = _DEFAULT_TIMEOUT, name: str = "") -> tuple:
    """GET `url` and parse JSON. Returns (status, data).

    status is one of:
      "ok"            -> data is the parsed body; validators stored in `state`
      "not_modified"  -> the server said 304; reuse what you cached
      "rate_limited"  -> throttled; `state["blocked_until"]` says until when
      "failed"        -> anything else; data is None

    `state` is a small per-URL dict the CALLER persists in its own cache
    (keys: etag, last_modified, blocked_until, fails). Keeping it caller-owned
    means this module holds no global state and stays trivially testable.

    Never raises. A scanner that cannot fetch must degrade, not take the
    perception pass down with it.
    """
    return _fetch(url, state=state, headers=headers, timeout=timeout,
                  name=name, parse=True)


def _fetch(url: str, *, state: dict, headers, timeout: float, name: str,
           parse: bool) -> tuple:
    now = time.time()
    blocked_until = num(state.get("blocked_until"))
    if blocked_until > now:
        return "rate_limited", None

    request_headers = dict(headers or {})
    # Conditional GET. An unchanged feed then costs a 304 and no body -- and on
    # GitHub a 304 is free against the quota.
    if state.get("etag"):
        request_headers["If-None-Match"] = str(state["etag"])
    if state.get("last_modified"):
        request_headers["If-Modified-Since"] = str(state["last_modified"])
    # Ask for compression; urllib does not, and these feeds are large.
    request_headers.setdefault("Accept-Encoding", "gzip")

    label = name or url
    try:
        req = urllib.request.Request(url, headers=request_headers)
        with opener().open(req, timeout=timeout) as resp:
            # Read one byte past the cap so an oversized body is detected
            # rather than silently truncated into a parse error.
            body = resp.read(_MAX_BODY_BYTES + 1)
            if len(body) > _MAX_BODY_BYTES:
                _record_failure(state, label,
                                f"body exceeds {_MAX_BODY_BYTES} bytes")
                return "failed", None
            if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                # Decompress with the same ceiling: a small gzip body can
                # expand without bound, which is the whole trick.
                try:
                    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    body = decompressor.decompress(body, _MAX_BODY_BYTES)
                    if decompressor.unconsumed_tail:
                        _record_failure(state, label, "gzip expands past the cap")
                        return "failed", None
                except Exception as exc:  # noqa: BLE001
                    _record_failure(state, label, f"gzip: {exc}")
                    return "failed", None
            text = decode_body(body)
            data = json.loads(text) if parse else text
            # Only overwrite validators on a real 200 with a parsed body. An
            # error page served as 200 would otherwise poison the cache with
            # its own ETag, and every later 304 would re-confirm the emptiness.
            etag = resp.headers.get("ETag")
            last_modified = resp.headers.get("Last-Modified")
            state["etag"] = etag if etag else None
            state["last_modified"] = last_modified if last_modified else None
            state["fails"] = 0
            state.pop("blocked_until", None)
            return "ok", data
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            state["fails"] = 0
            return "not_modified", None
        if exc.code in (429, 403):
            wait = _retry_after_seconds(exc.headers, now)
            if wait > 0:
                state["blocked_until"] = now + wait
                logger.warning(
                    "scanners: %s is rate limited (HTTP %d); backing off %.0fs "
                    "— this is NOT a quiet source", label, exc.code, wait,
                )
                return "rate_limited", None
        _record_failure(state, label, f"HTTP {exc.code}")
        return "failed", None
    except Exception as exc:  # noqa: BLE001 — a scanner degrades, never dies
        _record_failure(state, label, repr(exc))
        return "failed", None


def _retry_after_seconds(response_headers, now: float) -> float:
    """Seconds to wait, from Retry-After or X-RateLimit-Reset. 0 if neither."""
    retry_after = (response_headers or {}).get("Retry-After")
    if retry_after:
        seconds = num(retry_after, -1.0)
        if seconds >= 0:
            return min(seconds, 3600.0)
    reset_at = num((response_headers or {}).get("X-RateLimit-Reset"), 0.0)
    if reset_at > now:
        return min(reset_at - now, 3600.0)
    # A 403 with neither header is probably not a rate limit at all -- say so
    # by returning 0 and letting the caller record it as an ordinary failure.
    return 0.0


def _record_failure(state: dict, label: str, detail: str) -> None:
    fails = int(num(state.get("fails"))) + 1
    state["fails"] = fails
    if fails >= _FAILS_BEFORE_LOUD:
        logger.warning("scanners: %s has failed %d times in a row (%s)",
                       label, fails, detail)
    else:
        logger.debug("scanners: %s fetch failed (%s)", label, detail)


__all__ = ["opener", "decode_body", "num", "clamp01", "fetch_json",
           "fetch_text"]
