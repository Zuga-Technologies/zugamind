# Writing your own scanner

`scanners/_template.py` documents the contract for scanners that live *inside*
this package (the kind you'd PR back to core). This directory is the other
path: **a private scanner for your own workflow that never touches the
package at all.**

You don't need to fork ZugaMind or open a PR to watch your own Slack channel,
your own Jira board, your own internal API. Write a function, inject it, done.

## The pattern

`StreamRunner.__init__` takes an `extra_scanners` dict — any `scan_*`
callable you pass in runs every cycle alongside the shipped world-scanners,
with one difference: injected scanners **bypass habituation filtering** by
design (they're assumed to already know their own dedupe/rate-limiting, the
way both examples below do via on-disk caching + "only if new" checks).

```python
from stream.runner import StreamRunner
from slack_mentions import scan_slack_mentions
from jira_assigned import scan_jira_assigned

runner = StreamRunner(extra_scanners={
    "scan_slack_mentions": scan_slack_mentions,
    "scan_jira_assigned": scan_jira_assigned,
})
runner.run_daemon(interval=420)
```

That's the entire integration. No package changes, no PR, no core dependency
on Slack or Jira — your own two files plus this seven-line launcher.

## The contract (same as core scanners — see `scanners/_template.py`)

Your function:
1. Named `scan_<something>`, takes no arguments, returns `list[dict]`.
2. Each dict needs at minimum: `type` (str), `detail` (str, ≤280 chars),
   `novelty`/`relevance`/`urgency` (floats, 0-1).
3. Fail-silent — wrap external calls in try/except, return `[]` on error.
   A scanner that raises is still fail-closed at the caller, but a clean
   scanner keeps your logs readable.
4. Cache external calls. Both examples below cache to
   `data/scanner_cache/<name>.json` with a TTL matched to how fast the
   source actually changes — don't poll Slack/Jira every cycle if your
   interval is 60s, you'll burn rate limit for nothing.
5. Cap output at ~5 triggers per cycle. The workspace can only attend to
   one winner per cycle regardless — flooding it with 40 triggers just
   makes the competition noisier.

## Worked examples

- **`slack_mentions.py`** — polls a Slack channel for messages mentioning you
  (or any string you configure), turns unread mentions into triggers. Config:
  `SLACK_BOT_TOKEN`, `ZUGAMIND_SLACK_CHANNEL`.
- **`jira_assigned.py`** — polls a Jira Cloud project for issues assigned to
  you that aren't yet Done, turns each into a trigger. Config:
  `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `ZUGAMIND_JIRA_PROJECT`.
- **`scan_dirty_worktree.py`** — flags git repos that have had uncommitted
  changes for too long (the "wrote it, tested it, forgot to commit it before
  the session ended" failure mode). Config: `ZUGAMIND_WATCH_WORKTREES`,
  `ZUGAMIND_DIRTY_THRESHOLD_HOURS` (default 24).
- **`scan_process_conflict.py`** — flags when a configured app/process is
  currently running, so an autonomous harness can check "is a human actively
  in this thing right now?" before touching its files. Config:
  `ZUGAMIND_PROCESS_WATCH` (comma-separated `label:process_name` pairs).
- **`discord_activity.py`** — polls a Discord channel via the bot REST API for
  new messages, turns unseen ones into triggers. Config: `DISCORD_BOT_TOKEN`,
  `ZUGAMIND_DISCORD_CHANNEL`, optional `ZUGAMIND_DISCORD_MENTION` filter.
- **`news_rss.py`** — polls any RSS/Atom feeds you configure (wire outlets,
  industry trades, a competitor's blog) and turns unseen items into triggers.
  Same parsing approach as the shipped `scan_ai_labs` world-scanner, pointed
  at feeds you choose. Config: `ZUGAMIND_NEWS_FEEDS` (comma-separated URLs),
  optional `ZUGAMIND_NEWS_KEYWORDS` filter, `ZUGAMIND_NEWS_CACHE_TTL`. Note on
  "real time": RSS is poll-based, not push — most outlets publish within
  minutes, but this is near-real-time, not instant.
- **`x_activity.py`** — polls X's recent-search API for posts matching a query
  you configure. Config: `X_BEARER_TOKEN`, `ZUGAMIND_X_QUERY`, optional
  `ZUGAMIND_X_MAX_RESULTS` / `ZUGAMIND_X_CACHE_TTL`. **Cost note:** X API v2 is
  pay-per-use (~$0.005/post read as of 2026) — the defaults (60 min cache,
  15 results/poll) are tuned for ~$50/month, not maximum freshness. Read the
  module docstring's cost math before changing either default; both scale the
  bill roughly linearly. Verified against the live API before shipping.
- **`run_with_custom_scanners.py`** — the launcher shape above, runnable
  as-is once you've set the env vars for whichever example(s) you want.

All are stdlib-only (`urllib.request`/`subprocess`, matching the rest of
the package) — not a hard requirement for *your own* private scanner (only
core PRs are stdlib-constrained, see `CONTRIBUTING.md`), but it means you can
copy these and run them with zero `pip install`.

See also `examples/hooks/` — feeding what these scanners find into a session
you're already working in (not just a spawned headless one), via Claude Code's
own SessionStart/UserPromptSubmit hooks.

If you build something reusable — a scanner other integrators would want —
consider PRing it into `scanners/world/` for real, following
`scanners/_template.py`'s contract instead. That's the difference between
this directory and that one: private-to-you vs. shipped-to-everyone.
