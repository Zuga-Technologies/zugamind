"""Fleet failure_reason contract — ZugaMind's adoption (tier-1 sitting #7).

Law: docs/fleet/FAILURE_REASON_CONTRACT.md (taxonomy ruled 2026-08-11).
Slug format `<category>: <detail>`, whole slug <= 200 chars. A category may
only come from the ruled list — anything else is preserved as
`unknown: <original>`, never guessed into a category. None stays None
(nobody attempted classification; success rows stay NULL).

Unlike a fresh adopter (ZugaProving, ZugaNews, ...), ZugaMind already had a
local failure vocabulary before this contract existed — split across two
field names (`error` in act/command_actuator.py, `reason` in
gates/action_gate.py and stream/runner.py). `map_local_slug()` is the
CARRIER for that existing vocabulary: it translates ZugaMind's own local
slugs (rate_limited, budget_exhausted, api_error:<exc>, ...) into the fleet
taxonomy, one token at a time, ruled site-by-site by Buga 2026-08-12 (see
docs/fleet/failure-reason-rollout.md log entry "ZugaMind (#7) BUILD RULINGS
LOCKED"). The local slug is always kept as the DETAIL half of the result so
the old vocabulary stays greppable inside the new field.

Deliberate-skip states are EXCLUDED — they are not failures and never get a
failure_reason: `harness_disabled`, `dry_run`, `wake_filtered`,
`quiet_hours_deferred`, and any ok:True event (including the degraded-
success `budget_not_persisted:<exc>` reason — the response was already
paid for, so this is success-with-a-footnote, not a failure). Bi-compat:
the old key (`error`/`reason`) is written UNCHANGED alongside the new
`failure_reason` key at every write site; historical events are never
rewritten.

Stdlib-only.
"""

from __future__ import annotations

CATEGORIES = (
    "capacity", "incomplete", "infrastructure", "quality", "resource",
    "escalation", "unknown", "dependency", "auth", "rate_limit", "budget",
    "input", "internal",
)

MAX_LEN = 200

# Deliberate-skip / success-shaped local slugs (Buga's ruling, 2026-08-12):
# never a failure, never mapped, even if a future call site passes one of
# these in by mistake. `budget_not_persisted` is defense-in-depth — it's a
# `reason` on an ok:True result, so no call site should ever route it
# through this function, but excluding it here means a mistake at a call
# site degrades to "no failure_reason" instead of a false failure count.
_EXCLUDED_LOCAL = (
    "harness_disabled",
    "dry_run",
    "wake_filtered",
    "quiet_hours_deferred",
    "budget_not_persisted",
)

# local-slug PREFIX -> fleet category. A local string matches a prefix when
# it equals it exactly or starts with it (so "rate_limited" matches both the
# bare "rate_limited" and the enriched "rate_limited (hour cap: 3/3)"; the
# `<exc>`-suffixed slugs like "api_error:Connection refused" match via the
# same startswith check). `invoke_error` and `shield_refused` are handled
# separately below — their category (or detail) depends on more than a
# fixed lookup. Ruled by Buga 2026-08-12 (docs/fleet/failure-reason-rollout.md
# log, "ZugaMind (#7) BUILD RULINGS LOCKED").
_PREFIX_TO_CATEGORY = (
    ("rate_limit_indeterminate", "infrastructure"),
    ("rate_limited", "rate_limit"),
    ("budget_exhausted", "budget"),
    ("budget_error", "infrastructure"),
    ("api_error", "dependency"),
    ("runner_error", "internal"),
    ("empty_command", "input"),
    ("timeout", "resource"),
    ("import_error", "internal"),
    ("setup_error", "internal"),
    # Picked "internal" over "infrastructure" for consistency with
    # setup_error — both are OUR code failing to prepare a call, not the
    # environment being broken. Noted per the build ruling, not a
    # re-litigation of the category.
    ("can_spend_error", "internal"),
    ("requires_human_review", "escalation"),
    # Defensive fallback for stream/runner.py's `gate_result.get("reason",
    # "gate_not_ok")` — not currently reachable (action_gate always sets a
    # "reason" on every ok:False path) but ruled explicitly so a future gap
    # degrades to `unknown:`, never silently to None.
    ("gate_not_ok", "unknown"),
)

# Substrings that mean the harness binary/environment itself failed to
# start (invoke_error) rather than our own code throwing after a
# successful launch. Simple, honest substring check on the exception text
# — not full exception introspection, per the ruling.
#
# HONESTY NOTE (found during the build sitting): the local slug at the
# invoke_error write site is built as f"invoke_error:{e}", which calls
# str(e) — and Python's OSError.__str__ (FileNotFoundError's base class)
# does NOT include the class name. A real missing-binary error on this
# machine journals as "invoke_error:[WinError 2] The system cannot find
# the file specified" (verified against a live journal.jsonl row), so this
# check currently falls through to "internal:" for that case rather than
# firing "infrastructure:" — the check is implemented exactly as ruled
# (a literal substring match), but is honestly a rare-fire heuristic given
# the current text format, not a broken one. Making it fire on the common
# case would mean changing the write site to interpolate
# type(e).__name__ into the local slug — a live-vocabulary-format change
# outside this sitting's additive-only scope; left as a flagged follow-up.
_INVOKE_ENV_MARKERS = ("FileNotFoundError", "PermissionError")


def normalize(raw: str | None) -> str | None:
    """Carrier-law normalize: truncate to MAX_LEN, wrap any slug whose
    prefix isn't a ruled category as `unknown: <original>`. None stays
    None (nobody attempted classification)."""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    prefix = raw.split(":", 1)[0].strip().lower()
    if ":" in raw and prefix in CATEGORIES:
        # Rebuild from the CANONICAL prefix rather than returning `raw`
        # verbatim. The old code lowercased the prefix only to TEST it, then
        # emitted the original -- so "INTERNAL: boom" passed the membership
        # check and shipped a category that is not in CATEGORIES, which is the
        # one thing this module's docstring says cannot happen. Any fleet
        # consumer grouping by slug.split(":")[0] got a category that fails a
        # membership test (audit 2026-08-29). Normalising here also fixes the
        # shape for "internal:noSpace" and "internal   : padded".
        detail = raw.split(":", 1)[1].strip()
        return f"{prefix}: {detail}"[:MAX_LEN]
    return f"unknown: {raw}"[:MAX_LEN]


def map_local_slug(local: str | None) -> str | None:
    """Map one of ZugaMind's existing local `error`/`reason` slugs to the
    fleet `failure_reason` taxonomy.

    Returns None for a None input and for deliberate-skip/success-shaped
    local slugs (never a failure — Buga's 2026-08-12 ruling). Otherwise
    returns `<category>: <local>` (truncated/normalized), with `local`
    preserved verbatim as the detail. A local slug this table has no ruled
    entry for still gets a reason — `unknown: <local>` — never silently
    dropped: that's the fleet carrier law (a guessed category is worse than
    unknown, but a dropped one is worse than either).
    """
    if local is None:
        return None
    local = local.strip()
    if not local:
        return None
    if local in _EXCLUDED_LOCAL or local.startswith("budget_not_persisted:"):
        return None

    if local == "shield_refused" or local.startswith("shield_refused:"):
        # shield_refused:<detail> -> unknown: shield_refused:<detail> — no
        # forced category (Buga's ruling); promotable by future ruling.
        return normalize(f"unknown: {local}")

    if local == "invoke_error" or local.startswith("invoke_error:"):
        category = (
            "infrastructure"
            if any(marker in local for marker in _INVOKE_ENV_MARKERS)
            else "internal"
        )
        return normalize(f"{category}: {local}")

    for prefix, category in _PREFIX_TO_CATEGORY:
        if local == prefix or local.startswith(prefix):
            return normalize(f"{category}: {local}")

    return normalize(f"unknown: {local}")


__all__ = ["CATEGORIES", "MAX_LEN", "normalize", "map_local_slug"]
