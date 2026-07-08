# Example harness configs

Each file here is a ready-to-copy `act.command_actuator` config: a JSON list
of harness definitions. Copy the one(s) you want into a single file (or
merge them into one list) and point `ZUGAMIND_HARNESS_CONFIG` at it — or
just drop the merged file at `<repo>/zugamind/data/harness.json`, the
default location `command_actuator.load_harness_configs()` looks at when
the env var isn't set.

| File | Harness | Status |
|---|---|---|
| `claude-code.json` | [Claude Code](https://claude.com/claude-code) CLI (`claude -p ...`) | Verified — matches the CLI's documented non-interactive `-p` flag |
| `openclaw.json` | OpenClaw | **Community-unverified** — argv shape not tested against a real install |
| `hermes.json` | Hermes | **Community-unverified** — argv shape not tested against a real install |
| `generic-webhook.json` | Any HTTP-reachable automation | Verified as a `curl` invocation shape; you must supply your own URL |

## Config shape

```json
{
  "name": "claude-code",
  "command": ["claude", "-p", "Read the briefing at {briefing_file} and act on it"],
  "timeout_sec": 300,
  "max_per_hour": 4,
  "enabled": true
}
```

- `command` is a plain argv list. The literal substring `{briefing_file}`
  in any string element is replaced with the path to a temp file containing
  the cycle's markdown briefing (see `continuity.journal.build_briefing`).
  Passing the briefing as a **file path** rather than inlining its text
  keeps the argv short and avoids shell-quoting hazards; the prompt text
  simply tells the harness to go read it.
- `timeout_sec` bounds the subprocess call (`invoke_harness` never raises
  on timeout — it returns `{"ok": False, "error": "timeout"}`).
- `max_per_hour` is a rolling-hour rate limit counted from ZugaMind's own
  journal, independent of any budget cap the harness itself enforces.
- `enabled: false` lets you ship a config (for reference, or half-configured)
  without it ever actually running — `invoke_harness` refuses immediately.

The openclaw/hermes examples ship `enabled: false` for exactly this reason:
their argv shapes are best-effort guesses at each project's CLI, not
confirmed against a real install. If you run either project and can
confirm (or correct) the shape, a PR updating the `_comment` and flipping
`enabled` to `true` is welcome.
