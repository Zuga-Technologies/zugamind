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

import logging
import math
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


__all__ = ["opener", "decode_body", "num", "clamp01"]
