"""SCANNER TEMPLATE — copy this when authoring a new scanner.

A scanner watches one source (a file, a DB table, an HTTP endpoint, etc.)
and turns recent activity into cognitive triggers the workspace bid pass
will evaluate.

CONTRACT (every scanner module MUST follow):

  1. Module name: lowercase, descriptive, ends in `.py`. Examples:
       hackernews.py, reddit_ai.py, ai_labs.py.

  2. Exposes ONE public function: `scan_<thing>() -> list[dict]`.
     Function name MUST start with `scan_`. The package's dynamic loader
     discovers exports by name.

  3. Each returned trigger dict has at minimum:
       type:      str   (snake_case, prefix with the scanner's namespace)
       detail:    str   (short human-readable summary, ≤280 chars)
       novelty:   float (0..1)
       relevance: float (0..1)
       urgency:   float (0..1)

  4. OPTIONAL fields the trigger system understands:
       bypass_habituation: bool  (re-emit on 60min cooldown instead of
                                  HABITUATION_HOURS — set TRUE only for
                                  high-severity / high-priority signals
                                  the agent MUST keep seeing until acted)
       finding_severity:   str   (low|medium|high — used by sentinel
                                  prompt rule to prefer action=code)
       <any domain field>: any   (passed through to the workspace
                                  bidders + sentinel context)

  4b. (optional, advanced) LENS FIELDS. If your workspace/decision layer
      understands the extended what/why/who/where/when/how/problem/process/
      performance lens set, you may set any of them explicitly when your
      scanner knows a value better than a generic default (e.g. `where` to
      name an external domain the signal came from). All are optional — omit
      this section entirely for a simple scanner; the core type/detail/
      novelty/relevance/urgency fields above are sufficient on their own.

  5. Stdlib only when possible. If you must import from an optional
     internal package, do so inside the function so import failure of an
     optional dep doesn't break startup.

  6. ALWAYS fail-silent: wrap external calls in try/except, return [] on
     any error. The caller wraps the whole call too, but a clean
     scanner makes log noise lower.

  7. Cache external HTTP responses. The cognitive cycle can run every few
     minutes. Use data/scanner_cache/<name>.json with a TTL appropriate to
     the source (news ≈ 30min, arxiv-style feeds ≈ 1h, reddit ≈ 1h,
     internal DB ≈ 60s).

  8. Cap output. No scanner should return >5 triggers per cycle without
     a strong reason. The workspace can only attend to so many.

EXAMPLE SHAPE:

    def scan_<thing>() -> list[dict]:
        try:
            items = _fetch()
        except Exception:
            return []
        return [
            {
                "type": "<thing>_<event>",
                "detail": f"{item.title}",
                "novelty": 0.7,
                "relevance": 0.6,
                "urgency": 0.3,
            }
            for item in items[:5]
        ]

This file itself is a stub — it's skipped by the dynamic loader because
its public name does not start with `scan_`.
"""

# Intentionally no public scan_* function — this file is documentation only.
