"""ZugaMind continuity journal — episodic event log + harness wake briefing.

An agent harness (Claude Code, OpenClaw, Hermes, ...) is stateless between
invocations: it thinks when prompted, then forgets. This journal is
ZugaMind's side of the continuity the harness itself can't hold — every
notable cycle event (a workspace winner, a harness invocation, an alarm, a
handoff) is appended here in order, and `build_briefing()` turns the tail of
that record into the markdown a waking harness reads on its way in.

Storage: an append-only JSONL file at `<ENGINE_DIR>/journal.jsonl` (one JSON
object per line, oldest first). Stdlib-only (json + pathlib + datetime).

Fail-closed on write: `append_event()` must never be the reason a cognitive
cycle crashes, so any disk/serialization error is logged and swallowed.

Durability (audit 2026-08-28, against a live journal of 7,576 lines):
- Each event is ONE unbuffered write of the whole line, under a
  cross-process advisory lock (a sidecar `journal.lock`, `msvcrt.locking`
  on Windows / `fcntl.flock` elsewhere). Measured on this box: without
  the lock, two appenders lost ~1% of events with NO error and NO
  malformed line — Windows' C runtime implements "append" as seek-to-end
  then write, so two handles land on the same offset and one silently
  overwrites the other (POSIX O_APPEND is atomic; the CRT's is not).
  Different-length events overwrite partially, which is exactly the
  orphaned-tail shape the live journal carried (four of them).
- On a process's first append (and after a rotation) the file's last byte
  is checked: a torn tail with no newline used to glue the NEXT event onto
  it, losing both. Only a dead process leaves a torn tail, so once per
  process is exactly when it matters.
- `read_events` counts what it skips and says so (once per read).
- ROTATION: the file grew 3.9 MB in six weeks with nothing bounding it,
  and every read (the rate limiter per wake, the briefing per wake) parses
  the whole file. The journal is three files: the ACTIVE segment
  (`journal.jsonl`), the PREVIOUS segment (`journal.1.jsonl`) and the
  ARCHIVE segments (`journal.archive.<utc-stamp>.jsonl`, never deleted —
  the journal is episodic memory). Past `ZUGAMIND_JOURNAL_MAX_BYTES`
  (default 8 MB) the previous segment is RENAMED into the archive and the
  active file is RENAMED to become the previous segment; the next append
  creates a fresh active file. Every step is a rename: atomic, and either
  happens or is deferred — no copy can be half-done or done twice. A first
  version snapshotted-then-replaced the active file and erased any append
  that landed in between; a second copied-then-deleted and duplicated the
  copy when the delete was refused (both caught the same day). A writer
  that opened the old file mid-rename keeps writing into the renamed
  segment, which readers still parse. Readers see previous + active: at
  least one full segment of history, weeks at today's rate, far more than
  any reader's window (the rate limiter needs 24 h).
- `ts` and `kind` are the journal's own; a payload cannot overwrite them.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

try:  # Windows
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None  # type: ignore[assignment]
    import fcntl

from foundation.config import ENGINE_DIR
from foundation.state import load_state

logger = logging.getLogger("zugamind.continuity.journal")

JOURNAL_FILE: Path = ENGINE_DIR / "journal.jsonl"

# Rotation: past this many bytes the active segment is renamed to the
# previous segment (and the previous one archived). Checked every
# _ROTATE_CHECK_EVERY appends, not every append — a stat per write is cheap
# but pointless.
_DEFAULT_JOURNAL_MAX_BYTES = 8 * 1024 * 1024
_ROTATE_CHECK_EVERY = 100
_appends_since_check = 0
# A torn tail (a line with no trailing newline) is left only by a process that
# died mid-write, so it is checked on THIS process's first append and after a
# rotation — not on every write: the read-open it takes costs ~25 ms on a
# Windows box (scan-on-open after a write) against 0.3 ms for the append.
_tail_checked_for: Optional[Path] = None
# On Windows an open() on the journal fails with PermissionError for the few
# milliseconds another process is renaming it (rotation) or still holds it
# from its own append. Retry briefly instead of dropping the event.
_APPEND_ATTEMPTS = 60          # up to ~1.2 s of patience under pathological contention
_APPEND_RETRY_SLEEP_S = 0.02
# The cross-process lock: how long to wait for it before giving up on this
# append (logged and swallowed like any other append failure).
_LOCK_ATTEMPTS = 200
_LOCK_RETRY_SLEEP_S = 0.01


def lock_file() -> Path:
    return JOURNAL_FILE.with_name(JOURNAL_FILE.stem + ".lock")


@contextlib.contextmanager
def _journal_lock() -> Iterator[None]:
    """Exclusive, cross-process, advisory lock on the journal (sidecar file).
    Serialises appends and rotation across the daemon and any hand-run CLI.
    Raises TimeoutError if the lock cannot be taken in time."""
    lock_file().parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_file()), os.O_RDWR | os.O_CREAT)
    try:
        for attempt in range(_LOCK_ATTEMPTS):
            try:
                if msvcrt is not None:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if attempt == _LOCK_ATTEMPTS - 1:
                    raise TimeoutError("journal lock busy")
                time.sleep(_LOCK_RETRY_SLEEP_S)
        try:
            yield
        finally:
            try:
                if msvcrt is not None:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)


def _journal_max_bytes() -> int:
    try:
        return max(64 * 1024, int(os.environ.get("ZUGAMIND_JOURNAL_MAX_BYTES", _DEFAULT_JOURNAL_MAX_BYTES)))
    except (TypeError, ValueError):
        return _DEFAULT_JOURNAL_MAX_BYTES


def archive_files() -> List[Path]:
    """Archived segments, oldest first (never deleted)."""
    return sorted(JOURNAL_FILE.parent.glob(JOURNAL_FILE.stem + ".archive.*.jsonl"))


def archive_file() -> Path:
    """Back-compat: the NEWEST archive segment (or the name the next one
    would get). Prefer `archive_files()`."""
    files = archive_files()
    return files[-1] if files else JOURNAL_FILE.with_name(JOURNAL_FILE.stem + ".archive.0.jsonl")


def previous_segment() -> Path:
    return JOURNAL_FILE.with_name(JOURNAL_FILE.stem + ".1.jsonl")

# How many events a FIRST briefing (no last-wake cursor) considers. With a
# cursor the window is bounded by TIME, not lines: a briefing after a week
# idle used to report "2000 winner(s)" for 3000 and call an old handoff
# "none" because it fell past a 2000-line horizon (audit 2026-08-28).
# Handoffs are always scanned across the whole active journal.
_BRIEFING_SCAN_LIMIT = 2000

# Budget split of the size cap (ZUGAMIND_BRIEFING_MAX_CHARS) between the
# sections. The two "exempt" sections used to have no ceiling of their own:
# a winner with 20 triggers rendered ~6,750 chars on its own, so the final
# hard slice cut it mid-word and erased "Since last wake" and the handoffs
# entirely — the docstring's "never touched" was false. Each section is now
# trimmed to ITS share (whole lines, with a "+N more" marker) before the
# group cap shrinks and before any last-resort slice.
_WINNER_SHARE = 0.35
_DIGEST_SHARE = 0.30

# How many items to list per group in the rendered briefing, before any
# hard-cap truncation kicks in — keeps the whole briefing well under the
# ~80-line budget regardless of journal size in the common case.
_GROUP_DISPLAY_CAP = 5

# Hard ceiling on the rendered briefing's length, in characters, overridable
# via ZUGAMIND_BRIEFING_MAX_CHARS. Context assembly is exactly where
# ambient-cognition systems go wrong: an unbounded "everything that
# happened" dump drowns the waking harness's own context window in noise
# instead of orienting it. build_briefing() enforces this cap itself rather
# than trusting every caller to truncate downstream.
_DEFAULT_BRIEFING_MAX_CHARS = 4000
_TRUNCATION_SUFFIX = "\n\n_(older events trimmed to fit ZUGAMIND_BRIEFING_MAX_CHARS)_"


def now_iso() -> str:
    """Current UTC time as an ISO 8601 string. A module-level seam so tests
    can monkeypatch it to backdate synthetic journal entries."""
    return datetime.now(timezone.utc).isoformat()


def _ends_with_newline(path: Path) -> bool:
    """True for an empty/missing file or one whose last byte is a newline."""
    try:
        size = path.stat().st_size
    except OSError:
        return True
    if size == 0:
        return True
    with path.open("rb") as f:
        f.seek(-1, os.SEEK_END)
        return f.read(1) == b"\n"


def _rotate_if_needed() -> None:
    """Once the active segment exceeds the size cap: rename the previous
    segment into the archive, then rename the active segment to become the
    previous one. Renames only — nothing is copied, nothing another process
    may be appending to is ever rewritten, so a concurrent append can never
    be erased or duplicated: it lands either in the fresh active file or in
    the just-renamed segment, both of which readers parse. On Windows a
    rename fails while another process holds the file open (a 0.3 ms
    window per append); that just defers rotation to the next check. Never
    deletes a line."""
    global _appends_since_check, _tail_checked_for
    _appends_since_check += 1
    if _appends_since_check < _ROTATE_CHECK_EVERY:
        return
    _appends_since_check = 0
    try:
        if not JOURNAL_FILE.exists() or JOURNAL_FILE.stat().st_size <= _journal_max_bytes():
            return
        with _journal_lock():
            if not JOURNAL_FILE.exists():
                return  # another process rotated first
            prev = previous_segment()
            if prev.exists():
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
                os.rename(prev, JOURNAL_FILE.with_name(f"{JOURNAL_FILE.stem}.archive.{stamp}.jsonl"))
            os.rename(JOURNAL_FILE, prev)
            _tail_checked_for = None  # the next append starts the fresh active file
        logger.info("journal rotated: active segment -> %s", prev.name)
    except Exception as e:  # noqa: BLE001 — rotation is housekeeping, never fatal
        logger.warning("journal rotation deferred (non-fatal): %s", e)


def append_event(kind: str, payload: Dict[str, Any]) -> None:
    """Append one event: `{**payload, "ts": now_iso(), "kind": kind}`.

    `ts` and `kind` are the journal's own and win over a payload's keys —
    every `since_iso` window and the rate limiter depend on `ts` being
    exactly what now_iso() produces. The line is written with ONE unbuffered
    write; on this process's first append, if the file's tail is torn (no
    trailing newline) a newline goes first so this event never glues onto
    the remnant a dead process left behind.

    Best-effort and side-effect-free on failure: a full disk, a bad payload,
    or a permissions error is logged at WARNING and swallowed — journaling
    must never be the reason the caller's cycle breaks.
    """
    try:
        JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not isinstance(payload, dict):
            payload = {"payload": payload}
        event = {**payload, "ts": now_iso(), "kind": kind}
        line = json.dumps(event, default=str).encode("utf-8")
        if b"\n" in line:  # json.dumps escapes newlines inside strings; belt and braces
            line = line.replace(b"\n", b" ")
        global _tail_checked_for
        with _journal_lock():
            prefix = b""
            if _tail_checked_for != JOURNAL_FILE:
                prefix = b"" if _ends_with_newline(JOURNAL_FILE) else b"\n"
                _tail_checked_for = JOURNAL_FILE
            for attempt in range(_APPEND_ATTEMPTS):
                try:
                    with JOURNAL_FILE.open("ab", buffering=0) as f:
                        f.write(prefix + line + b"\n")
                    break
                except PermissionError:
                    if attempt == _APPEND_ATTEMPTS - 1:
                        raise
                    time.sleep(_APPEND_RETRY_SLEEP_S)
        _rotate_if_needed()
    except Exception as e:  # noqa: BLE001 — journaling is best-effort, never fatal
        logger.warning("journal append failed (non-fatal): %s", e)


def read_events(
    since_iso: Optional[str] = None,
    limit: int = 200,
    *,
    on_error: str = "empty",
) -> List[Dict[str, Any]]:
    """Return journal events in chronological order (oldest first), across
    the previous and active segments (see the module docstring on rotation).

    Args:
        since_iso: if given, only events with `ts` strictly greater than
                   this ISO string are returned (both compare as strings —
                   safe because every `ts` is produced by `now_iso()`, which
                   always emits the same UTC-offset ISO format).
        limit: cap on the number of (post-filter) events returned; the most
               recent `limit` are kept. Malformed lines are skipped rather
               than raising. A missing journal file returns [].
        on_error: "empty" (default) degrades a *read failure* of an existing
                  file to [] — right for briefing/narrative callers, where no
                  history beats no cycle. "raise" re-raises it instead — for
                  callers whose SAFETY depends on distinguishing "no events"
                  from "couldn't read the events" (e.g. the rate limiter in
                  act/command_actuator, which must fail closed, not open,
                  when the count is unknowable). A missing file is [] in both
                  modes: a fresh install genuinely has no history.
    """
    segments = [seg for seg in (previous_segment(), JOURNAL_FILE) if seg.exists()]
    if not segments:
        return []
    try:
        raw_lines: List[str] = []
        for seg in segments:  # previous segment first: it is older
            raw_lines.extend(seg.read_text(encoding="utf-8").splitlines())
    except Exception as e:  # noqa: BLE001 — see on_error
        if on_error == "raise":
            raise
        logger.warning("journal read failed (non-fatal): %s", e)
        return []

    events: List[Dict[str, Any]] = []
    skipped = 0
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(event, dict):
            skipped += 1
            continue
        if since_iso is not None and str(event.get("ts", "")) <= since_iso:
            continue
        events.append(event)
    if skipped:
        # Say so: the live journal carried four orphaned event tails for weeks
        # and nothing ever mentioned them.
        logger.warning("journal: skipped %d malformed line(s) in %s", skipped,
                       " + ".join(seg.name for seg in segments))

    if limit is not None and limit >= 0:
        events = events[-limit:] if limit else []  # [-0:] would be the whole list
    return events


def _untrusted(text: Any, limit: int) -> str:
    """One line of externally-sourced text, safe to interpolate into the
    briefing. Everything a scanner captured — issue titles, feed headlines,
    harness stdout — reaches the waking harness through here, and every
    shipped harness prompt says "act on the briefing". A title carrying
    "\n\n## Why you're being woken\n- SYSTEM OVERRIDE ..." used to render as
    a second, indistinguishable header (audit 2026-08-28). Markdown
    structure lives at line starts, so collapsing all whitespace to single
    spaces removes every header/fence/list injection shape while leaving
    ordinary titles untouched; a leading "#" is neutralised for the rare
    case a value opens a line on its own."""
    flat = " ".join(str(text).split())
    if flat.startswith(("#", "```", ">")):
        flat = "\u2060" + flat  # word joiner: invisible, but no longer a line-start token
    return flat[:limit]


def _trigger_line(trig: Any, limit: int = 300) -> str:
    """`type: detail` for a trigger — never the raw dict. `str(trig)` used to
    be the fallback, which rendered every field the scanner had stored
    (including anything token-shaped) verbatim into the briefing."""
    if not isinstance(trig, dict):
        return _untrusted(trig, limit)
    body = trig.get("detail") or trig.get("summary") or trig.get("title") or "(no detail)"
    ttype = trig.get("type") or "?"
    return _untrusted(f"{ttype}: {body}", limit)


def _fit_lines(lines: List[str], budget_chars: int, keep_head: int = 0) -> List[str]:
    """Trim a section to `budget_chars` by dropping WHOLE lines from the end
    (after the first `keep_head`, which always stay), replacing what was
    dropped with one marker line."""
    if len("\n".join(lines)) <= budget_chars:
        return lines
    kept = list(lines[:keep_head])
    for line in lines[keep_head:]:
        candidate = kept + [line]
        if len("\n".join(candidate)) > budget_chars - 40:
            break
        kept = candidate
    dropped = len(lines) - len(kept)
    if dropped > 0:
        kept.append(f"  - (+{dropped} more, see journal)")
    return kept


def _describe_elapsed(since_iso: str, now: Optional[datetime] = None) -> str:
    """Human-readable elapsed time between `since_iso` and `now` (defaults
    to the real current time — injectable so briefings are testable). A
    naive timestamp is read as UTC: every cursor the runner writes comes
    from now_iso(), which is UTC — a naive value only arrives from a
    hand-edited state file, and reading it as local time reported a 5-minute
    gap as "5h 5m" on a UTC-5 box."""
    try:
        then = datetime.fromisoformat(since_iso)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        current = now if now is not None else datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        delta = current - then
        total_minutes = max(0, int(delta.total_seconds() // 60))
        hours, minutes = divmod(total_minutes, 60)
        if hours:
            return f"{hours}h {minutes}m"
        if total_minutes:
            return f"{total_minutes}m"
        return "<1m"
    except Exception:
        return "unknown"


def _max_briefing_chars() -> int:
    try:
        return int(os.environ.get("ZUGAMIND_BRIEFING_MAX_CHARS", _DEFAULT_BRIEFING_MAX_CHARS))
    except (TypeError, ValueError):
        return _DEFAULT_BRIEFING_MAX_CHARS


def _wake_gate_hints(winner_module: Optional[str],
                     harnesses: Optional[List[str]] = None) -> List[tuple]:
    """`[(harness, floor, basis), ...]` — the gates this winner was ACTUALLY
    judged by: enabled harnesses in "calibrate" mode, restricted to the
    names in `harnesses` when the caller says which ones are dispatching,
    and to those whose `wake_modules` allowlist admits the winner's module.
    It used to report the first calibrate-mode harness in the file, full
    stop — so a world_signals wake could be described against a
    repo_issues-only harness's 0.9 floor it never faced (audit 2026-08-28).

    Lazily imported and best-effort on purpose: `act.floor_calibration`
    imports THIS module, so a top-level import here is circular, and a
    briefing must still render if the act/ layer is missing entirely."""
    hints: List[tuple] = []
    try:
        from act import command_actuator, floor_calibration
        for hc in command_actuator.load_harness_configs():
            name = hc.get("name", "")
            if not hc.get("enabled", True) or hc.get("wake_min_salience") != "calibrate":
                continue
            if harnesses is not None and name not in harnesses:
                continue
            modules = hc.get("wake_modules")
            if isinstance(modules, list) and modules and winner_module not in modules:
                continue
            floor, basis = floor_calibration.resolve_gate(name)
            hints.append((name, floor, basis))
    except Exception as e:  # noqa: BLE001 — a hint must never break the briefing
        logger.debug("briefing: wake gate hint unavailable: %s", e)
    return hints


def _wake_gate_hint() -> Optional[tuple]:
    """Back-compat seam: `(floor, basis)` of the first applicable
    calibrate-mode harness, or None. build_briefing consults it only when
    `_wake_gate_hints` found nothing, so a caller (or test) that replaces
    this single-gate hook still steers the note."""
    hints = _wake_gate_hints(None, None)
    return (hints[0][1], hints[0][2]) if hints else None


def build_briefing(
    since_iso: Optional[str],
    winner: Optional[Dict[str, Any]] = None,
    *,
    other_criticals: Optional[List[Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
    harnesses: Optional[List[str]] = None,
) -> str:
    """Render a markdown wake briefing for a harness invocation.

    Args:
        since_iso: ISO timestamp of the last wake (the journal cursor), or
                   None if this is the first briefing ever built. Also used
                   to compute "time since last wake".
        winner: the workspace winner that triggered THIS wake — typically
                `WorkspaceContent.to_dict()` — or None for a non-winner-
                triggered wake (e.g. a scheduled/manual run).
        now: override for "the current time", for deterministic tests. Real
             callers should omit it.
        harnesses: names of the harness configs this briefing is about to be
             dispatched to, so the "was this wake earned" note quotes the
             gate(s) the winner actually faced. None = every enabled
             calibrate-mode harness whose wake_modules admit the winner.

    Sections: current cognitive state + time since last wake; the winning
    trigger that caused this wake; recent notable events since the last
    wake grouped into winners / actions taken / alarms / deferred-during-
    quiet-hours; and unresolved handoffs (kind "handoff" events with no
    matching "handoff_done").

    HARD SIZE CAP: the rendered briefing is truncated to at most
    `ZUGAMIND_BRIEFING_MAX_CHARS` characters (default 4000). Context
    assembly is exactly where "ambient cognition" implementations tend to
    go wrong — an unbounded dump of everything that happened between wakes
    drowns the waking harness's own context window in noise instead of
    orienting it. When the natural render is too long, the OLDEST displayed
    item in each group is dropped first (one round at a time, across all
    groups together, since groups are already newest-last); the current
    winner's "Why you're being woken" section is never touched — it's the
    one piece of context this briefing exists to deliver.

    Deterministic given the journal's contents, `now`, and the current
    state / harness-config / floor-calibration files (all three are read):
    no randomness, no hidden clock reads beyond the one explicit `now` (or,
    if omitted, a single read of the real clock for the elapsed-time line).
    All externally-sourced text is rendered through `_untrusted` — one line,
    no markdown structure — so scanned content cannot forge a section.
    """
    try:
        state = load_state()
    except Exception as e:  # noqa: BLE001 — a broken state file must not break the briefing
        logger.debug("briefing: state load failed: %s", e)
        state = {"state": "UNKNOWN"}

    if since_iso:
        # Bounded by TIME: everything since the cursor, however many events.
        recent_events = read_events(since_iso=since_iso, limit=None)
    else:
        recent_events = read_events(since_iso=None, limit=_BRIEFING_SCAN_LIMIT)
    # Handoffs are rare and long-lived: resolve them across the whole active
    # journal, never a line-count window.
    all_events = read_events(since_iso=None, limit=None)

    winners = [e for e in recent_events if e.get("kind") == "cycle" and e.get("winner")]
    actions = [e for e in recent_events if e.get("kind") == "harness_invocation"]
    alarms = [e for e in recent_events if e.get("kind") == "alarm"]
    deferred = [e for e in recent_events if e.get("kind") == "quiet_hours_deferred"]

    handoff_done_ids = {
        e.get("id") for e in all_events if e.get("kind") == "handoff_done" and e.get("id")
    }
    unresolved = [
        e for e in all_events
        if e.get("kind") == "handoff" and e.get("id") not in handoff_done_ids
    ]

    max_chars = _max_briefing_chars()

    def _render(group_cap: int) -> str:
        lines: List[str] = ["# ZugaMind Wake Briefing", ""]
        lines.append(f"**Cognitive state:** {_untrusted(state.get('state', 'UNKNOWN'), 20)}")
        if since_iso:
            lines.append(f"**Time since last wake:** {_describe_elapsed(since_iso, now)}")
        else:
            lines.append("**Time since last wake:** (no prior wake recorded — first briefing)")

        lines.append("")
        lines.append("## Why you're being woken")
        winner_lines: List[str] = []
        if winner:
            module = _untrusted(winner.get("source_module", "?"), 60)
            content = _untrusted(winner.get("content", ""), 200)
            salience = winner.get("salience")
            sal_str = f"{salience:.2f}" if isinstance(salience, (int, float)) else "?"
            winner_lines.append(f"- **{module}** (salience {sal_str}): {content}")
            if (winner.get("context") or {}).get("alarm_lane"):
                # The salience above is the module's dampened chatter score;
                # the lane chose this bid because a trigger carried urgency
                # >= 0.9. Without this line the woken session saw a 0.15
                # "routine" winner and no alarm anywhere.
                winner_lines.append("  ALARM-LANE CRITICAL: selected by the alarm lane, not the lottery. "
                                    "The salience number is dampened module chatter, not the alert's "
                                    "urgency — treat as urgent.")
            # Was this wake EARNED, or did the attention schema's
            # monopoly-breaking multipliers carry it over the bar? Those
            # multipliers exist to share attention INSIDE the mind; the
            # briefing showed only their output, so on 2026-08-17 two
            # separate wakes (17:54 and 18:34) each spent a whole paid
            # session re-deriving the answer from journal.jsonl by hand.
            # Same lesson as the Link line below: the one number the woken
            # session needs to judge its own wake belongs HERE, next to the
            # salience it would otherwise take at face value.
            raw = (winner.get("context") or {}).get("raw_salience")
            if (isinstance(raw, (int, float)) and isinstance(salience, (int, float))
                    and abs(raw - salience) > 1e-9):
                note = (f"  Bid {raw:.2f}, woke on {salience:.2f} "
                        f"after attention-health modulation")
                gates = _wake_gate_hints(winner.get("source_module"), harnesses)
                if not gates:
                    legacy = _wake_gate_hint()
                    if legacy:
                        gates = [("", legacy[0], legacy[1])]
                if gates:
                    # Report the number the gate ACTUALLY compared. It judges
                    # min(bid, modulated) whenever the floor self-calibrates —
                    # the only case this hint reports — so naming a single
                    # basis ("judged on raw") understated it, and on 2026-08-18
                    # that wording described a wake as gate-approved on 0.67
                    # while the mind had damped the same signal to 0.25.
                    judged = min(raw, salience)
                    parts = [(f"{_untrusted(name, 40)}: " if name else "")
                             + f"bar {gate_floor:.3f} fitted on the {basis} series"
                             for name, gate_floor, basis in gates[:3]]
                    note += ("; ".join(f" ({p}" if i == 0 else p for i, p in enumerate(parts))
                             + f"; judged on min(bid, modulated) = {judged:.2f})")
                winner_lines.append(note + ".")
            # For an external signal the URL *is* the payload — the content
            # line is only a headline ("HN [202pts]: On AI regulation and
            # messaging"). Dropping it forced the 2026-08-17 17:45 wake to
            # grep journal.jsonl to learn what the signal even pointed at.
            # Same exemption from size-cap trimming as the rest of this
            # section, so it's capped per-line here.
            top_url = (winner.get("context") or {}).get("top_url")
            if top_url:
                winner_lines.append(f"  Link: {_untrusted(top_url, 300)}")
            # A winning bid can batch several triggers; the content line above
            # carries only the module's summary of the first/hottest one.
            # Enumerate every trigger so nothing that won attention is lost
            # before it reaches the model (EXP-001 finding: a canary won the
            # competition but its id never entered the briefing — issue #9).
            # This section is exempt from the size-cap trimming below, so cap
            # per-line and per-list here instead of trusting the global cap.
            triggers = (winner.get("context") or {}).get("triggers") or []
            first_detail = (_untrusted(triggers[0].get("detail", ""), 300)
                            if triggers and isinstance(triggers[0], dict) else "")
            if len(triggers) > 1 or (triggers and first_detail not in content):
                winner_lines.append("  Triggers in this bid:")
                for trig in triggers[:20]:
                    winner_lines.append(f"  - {_trigger_line(trig)}")
                if len(triggers) > 20:
                    winner_lines.append(f"  - (+{len(triggers) - 20} more, see journal)")
            # This section is exempt from the group-cap shrink below, so it
            # gets its own share of the cap: the first line (the winner) and
            # the earned-note always stay; trigger lines drop from the end.
            lines.extend(_fit_lines(winner_lines, int(max_chars * _WINNER_SHARE), keep_head=2))
        else:
            lines.append("- (no winner supplied — scheduled/manual wake)")

        # Critical digest: a page lists every active alert, not just the
        # loudest. With more concurrent alarms than wake slots, alarms that
        # lost this cycle's selection would otherwise queue past their
        # window (EXP-001 acceptance finding) — ride them along on this
        # wake. Same exemption from size-cap trimming as the winner section,
        # so capped per-line and per-list here.
        if other_criticals:
            lines.append("")
            lines.append("## Other active alarms (did not win this cycle)")
            digest: List[str] = []
            shown = 0
            for bid in other_criticals[:6]:
                mod = _untrusted(bid.get("source_module", "?"), 40)
                for trig in ((bid.get("context") or {}).get("triggers") or [])[:5]:
                    digest.append(f"- [{mod}] {_trigger_line(trig)}")
                    shown += 1
            total = sum(
                len((b.get("context") or {}).get("triggers") or [])
                for b in other_criticals
            )
            if total > shown:
                digest.append(f"- (+{total - shown} more, see journal)")
            lines.extend(_fit_lines(digest, int(max_chars * _DIGEST_SHARE)))

        lines.append("")
        lines.append("## Since last wake")
        lines.append(
            f"- {len(winners)} workspace winner(s), {len(actions)} harness invocation(s), "
            f"{len(alarms)} alarm(s), {len(deferred)} deferred (quiet hours)"
        )

        if group_cap > 0 and winners:
            lines.append("")
            lines.append("### Winners")
            for e in winners[-group_cap:]:
                w = e.get("winner") or {}
                lines.append(f"- [{e.get('ts', '?')}] {_untrusted(w.get('source_module', '?'), 40)}: "
                             f"{_untrusted(w.get('content', ''), 120)}")

        if group_cap > 0 and actions:
            lines.append("")
            lines.append("### Actions taken")
            for e in actions[-group_cap:]:
                status = "ok" if e.get("ok") else "FAILED"
                dry = " (dry-run)" if e.get("dry_run") else ""
                lines.append(f"- [{e.get('ts', '?')}] {_untrusted(e.get('harness', '?'), 40)}: {status}{dry}")

        if group_cap > 0 and alarms:
            lines.append("")
            lines.append("### Alarms")
            for e in alarms[-group_cap:]:
                lines.append(f"- [{e.get('ts', '?')}] {_untrusted(e.get('detail', e.get('reason', '?')), 200)}")

        if group_cap > 0 and deferred:
            lines.append("")
            lines.append("### Deferred during quiet hours")
            for e in deferred[-group_cap:]:
                w = e.get("winner") or {}
                lines.append(f"- [{e.get('ts', '?')}] {_untrusted(e.get('harness', '?'), 40)} <- "
                             f"{_untrusted(w.get('source_module', '?'), 40)}: {_untrusted(w.get('content', ''), 100)}")

        lines.append("")
        lines.append("## Unresolved handoffs")
        if group_cap > 0 and unresolved:
            for e in unresolved[-group_cap:]:
                lines.append(f"- [{e.get('ts', '?')}] {_untrusted(e.get('id', '?'), 60)}: "
                             f"{_untrusted(e.get('detail', ''), 200)}")
        else:
            lines.append("- none" if not unresolved else f"- {len(unresolved)} pending (trimmed — see journal)")

        return "\n".join(lines)

    cap = _GROUP_DISPLAY_CAP
    text = _render(cap)
    while len(text) > max_chars and cap > 0:
        cap -= 1
        text = _render(cap)

    if len(text) > max_chars:
        # Should only happen if the protected header itself is huge (e.g. an
        # absurd winner content) — hard-slice as the last resort, but the
        # winner's own content is already capped to 200 chars above.
        text = text[: max(0, max_chars - len(_TRUNCATION_SUFFIX))] + _TRUNCATION_SUFFIX

    return text


__all__ = ["append_event", "read_events", "build_briefing", "now_iso", "JOURNAL_FILE",
           "archive_files", "archive_file", "previous_segment", "lock_file"]
