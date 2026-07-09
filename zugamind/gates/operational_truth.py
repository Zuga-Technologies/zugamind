"""Freshness gate — verify operational claims against LIVE state.

An autonomous agent can confabulate: a true-once observation ("port 8000
down", "the cache uses 400MB") gets fed forward through an injected
prior-reflection memory block and re-narrated as still-true ("hasn't improved
despite previous attempts"). Scanners are fresh every cycle; the staleness is
the agent reading its own past as present — a stale claim about a metric that
was true once but is no longer verified live.

Ground + correct (the two halves of this gate):
  - snapshot():           cheap stdlib probe of live operational facts (which
                          known service ports are actually LISTENing), TTL-cached
                          so it costs ~nothing on the per-cycle reflection path.
  - format_block():       a timestamped VERIFIED-LIVE-STATE block injected into
                          the reflection prompt so the model grounds on truth.
  - is_stale_operational(): True when a prior reflection asserts an operational
                          status the live probe contradicts — used to DROP that
                          prior from the injected memory so the ghost can't
                          regenerate (a hard cut a soft prompt-ban can't make).

Stdlib-only (socket + re + time), fail-OPEN: any probe error returns an empty/
safe result so reflection is never blocked. Reflection > freshness.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import time
from datetime import datetime

# Ports the agent reasons about. These are probed for the grounding block. Only
# a port in THIS map can ever be used to contradict a prior — we never call a
# claim stale about something we didn't actually probe.
#
# Illustrative example only — a deployer should populate this with their own
# service map (mirrors foundation.config's empty LOCAL_SERVICES).
_KNOWN_PORTS = {
    8000: "example-api",
    8001: "example-web",
}

_DOWN_WORDS = (
    "down", "offline", "unavailable", "not responding", "unreachable",
    "is dead", "remains down", "still down", "went down",
)

_TTL_S = 30.0
_cache: dict | None = None
_cache_at: float = 0.0


def _port_up(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def snapshot(force: bool = False) -> dict:
    """{'ts': 'HH:MM:SS', 'ports': {8000: True, ...}}. TTL-cached. Never raises."""
    global _cache, _cache_at
    now = time.time()
    if not force and _cache is not None and (now - _cache_at) < _TTL_S:
        return _cache
    ports: dict[int, bool] = {}
    try:
        for p in _KNOWN_PORTS:
            ports[p] = _port_up(p)
    except Exception:
        pass
    _cache = {"ts": datetime.now().strftime("%H:%M:%S"), "ports": ports}
    _cache_at = now
    return _cache


def format_block(snap: dict | None = None) -> str:
    """Timestamped live-state grounding block for the reflection prompt.

    Empty string if the probe yielded nothing (fail-open — no block beats a
    misleading one)."""
    snap = snap or snapshot()
    ports = snap.get("ports") or {}
    if not ports:
        return ""
    up = [f"{_KNOWN_PORTS.get(p, '?')}(:{p})" for p, ok in sorted(ports.items()) if ok]
    down = [f"{_KNOWN_PORTS.get(p, '?')}(:{p})" for p, ok in sorted(ports.items()) if not ok]
    lines = [f"VERIFIED LIVE STATE (probed now, {snap.get('ts', '?')}):"]
    lines.append(f"  services UP: {', '.join(up) if up else '(none)'}")
    if down:
        lines.append(f"  services DOWN: {', '.join(down)}")
    lines.append(
        "  HARD RULE: do NOT claim a service is down, or cite a memory/leak "
        "figure, unless it appears above. A memory of a past outage is NOT "
        "current truth — this block is. Do not invent a metric this runtime "
        "doesn't expose."
    )
    return "\n".join(lines)


_PORT_RE = re.compile(r":?(\d{4})\b")
_PERCYCLE_RE = re.compile(r"per[\s-]*cycle", re.I)
_MEM_RE = re.compile(r"\b\d+\s*(?:mb|gb)\b|\bmemory\b|\bleak\b", re.I)
# General grounding: a problem asserted about a concrete absolute file path
# that does NOT exist on disk is confabulated — catches confab the enumerated
# classes miss (e.g. "high memory in /.../python@3.9/.../db.py" when no such
# file/process exists). Conservative: needs a problem word AND a cited dead
# path, and skips URL paths.
_PROBLEM_RE = re.compile(
    r"\b(error|warning|warn|fail|failing|failed|broken|leak|missing|crash|bloat|"
    r"high memory|memory warning)\b",
    re.I,
)
_ABS_PATH_RE = re.compile(r"(?<!:)(?:/[\w.@+-]+){2,}")


def is_stale_operational(text: str, snap: dict | None = None) -> bool:
    """True if `text` asserts an operational status the live probe disproves.

    Two confabulation classes are caught — conservatively (only on a clear
    contradiction), so a genuinely-down service or a real metric survives:
      1. names a KNOWN port + a down-word, but that port is actually LISTENing.
      2. a 'per cycle' memory/MB claim — categorically bogus when the runtime
         exposes no such metric, so any reflection citing one is confabulated.
      3. a problem asserted about a concrete absolute path that doesn't exist
         on disk.
    Never raises.
    """
    try:
        t = (text or "").lower()
        if not t:
            return False
        # class 2 — per-cycle memory confabulation.
        if _PERCYCLE_RE.search(t) and _MEM_RE.search(t):
            return True
        # class 3 — ungrounded path claim: a problem asserted about a concrete
        # absolute path that doesn't exist on disk. General (not tied to any
        # specific subsystem): the agent can't have a "memory warning" in a
        # file that isn't there.
        if _PROBLEM_RE.search(t):
            for m in _ABS_PATH_RE.finditer(text or ""):
                p = m.group(0)
                # skip URL paths and trivially short matches; only flag a path
                # long/specific enough to be a real claim that's verifiably absent
                if "//" in p or len(p) < 12:
                    continue
                try:
                    if not os.path.exists(p):
                        return True
                except Exception:
                    continue
        # class 1 — "port X down" while X is actually up
        if any(w in t for w in _DOWN_WORDS):
            snap = snap or snapshot()
            ports = snap.get("ports") or {}
            for m in _PORT_RE.finditer(t):
                if ports.get(int(m.group(1))) is True:
                    return True
    except Exception:
        return False
    return False


# ── Emit-time live re-probe (sensor confabulation) ──────────────────────────
# A code-built FACT is only as true as the scanner that produced it. Between the
# scan and the post, a metric can go stale. For the cheaply-probeable classes we
# RE-VERIFY against live state the instant before surfacing, so a stale-but-real
# datum ("15.4GB free" minutes ago, now 2GB) is dropped instead of broadcast.
# Fail-OPEN by design: anything we can't probe returns ok — reflection must never
# die on a probe miss, and we only ever DROP on a probe we actually ran.
_SRC_WHERE_RE = re.compile(r"\[src:\s*([^\]]+)\]", re.I)
_FREE_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(gb|mb|tb)\b", re.I)


def _gb(val: float, unit: str) -> float:
    u = unit.lower()
    return val * (1024.0 if u == "tb" else 1.0 if u == "gb" else 1.0 / 1024.0)


def verify_fact_live(fact_text: str, mount: str = "/", tol: float = 0.25) -> tuple[bool, str]:
    """Re-probe the probeable classes of a FACT at emit time.

    Returns (ok, reason). Covered class:
      - host disk-free: a "<N>GB ... free" claim cited to a host:* locus is
        re-read live via shutil.disk_usage(mount); dropped if it diverges from
        the claim by more than `tol` (fractional).
    Everything else (external/non-host loci, non-disk metrics) is unverifiable
    locally -> (True, 'unverifiable_class'). Fail-open on any error.

    Note: this deliberately does NOT os.path.exists() arbitrary paths parsed out
    of the FACT — that turns the surface decision into a filesystem-existence
    oracle over model/scanner-influenced strings. The citation gate already
    proves the FACT came from a real trigger this cycle, so path probing adds no
    grounding, only an information-disclosure surface.
    """
    try:
        t = fact_text or ""
        low = t.lower()
        where = ""
        mw = _SRC_WHERE_RE.search(t)
        if mw:
            where = mw.group(1).split()[0].strip().lower()  # first token = locus

        # host disk-free re-probe
        if where.startswith("host") and "free" in low:
            m = _FREE_NUM_RE.search(low)
            if m:
                claimed = _gb(float(m.group(1)), m.group(2))
                try:
                    live = shutil.disk_usage(mount).free / (1024.0 ** 3)
                except Exception:
                    return True, "disk_unprobeable"
                if claimed > 0 and abs(live - claimed) / claimed > tol:
                    return False, f"stale_disk:{claimed:.1f}gb_claimed_vs_{live:.1f}gb_live"
                return True, "disk_fresh"

        return True, "unverifiable_class"
    except Exception:
        return True, "verify_error"
