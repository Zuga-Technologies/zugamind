# Example harness configs

Each file here is a ready-to-copy `act.command_actuator` config: a JSON list
of harness definitions. Copy the one(s) you want into a single file (or
merge them into one list) and point `ZUGAMIND_HARNESS_CONFIG` at it — or
just drop the merged file at `<repo>/zugamind/data/harness.json`, the
default location `command_actuator.load_harness_configs()` looks at when
the env var isn't set.

Every row marked **verified end-to-end** passed the same live test on
2026-07-08 via `scripts/verify_harness.py` (nothing mocked): a canary
trigger won the workspace, cleared the action gate, the actuator spawned
the real harness binary, and the woken agent read the briefing and echoed
the canary token back. Run the same proof against your own setup:
`python scripts/verify_harness.py`.

| File | Harness | Status |
|---|---|---|
| `claude-code.json` | [Claude Code](https://claude.com/claude-code) 2.1.204 (`claude -p ...`) | **Verified end-to-end** (Windows) |
| `openclaw.json` | [OpenClaw](https://github.com/openclaw/openclaw) 2026.3.11 | **Verified end-to-end** (macOS) — the explicit `--session-id` is required |
| `codex.json` | [Codex CLI](https://github.com/openai/codex) 0.143.0 | **Verified end-to-end** (macOS) |
| `hermes.json` | [Hermes Agent](https://github.com/nousresearch/hermes-agent) 0.18.1 | **Verified end-to-end** (macOS, local Ollama qwen3:14b — a $0 wake path) |
| `generic-webhook.json` | Any HTTP-reachable automation | Verified as a `curl` invocation shape; you must supply your own URL |

## Config shape

```json
{
  "name": "claude-code",
  "command": ["claude", "-p", "Read the briefing at {briefing_file} and act on it"],
  "timeout_sec": 300,
  "max_per_hour": 4,
  "max_per_day": 20,
  "enabled": false
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
- `max_per_hour` / `max_per_day` are independent rolling rate limits
  counted from ZugaMind's own journal (so they survive restarts), separate
  from any budget cap the harness itself enforces. If the journal exists
  but can't be read, the actuator refuses the wake (`rate_limit_indeterminate`)
  rather than treating the count as zero — fail closed.
- **`enabled: false` is how every config here ships.** Copying a file must
  never be enough to hand an agent a live wake path driven by scraped
  internet content; flipping the flag to `true` is always an explicit act,
  done after you've read the `command` it will run.
- Optional per-harness wake filters, consumed by `stream/runner.py`:
  `"wake_modules": ["repo_issues"]` wakes this harness only when one of the
  named workspace modules wins, and `"wake_min_salience": 0.6` sets a
  salience floor. Without a filter a harness wakes for **every** gated
  winner — including ambient ones — which is the heartbeat-spam failure
  mode this sidecar exists to avoid. Set one before running unattended.
- `"wake_min_salience": "calibrate"` (a string, not a number) opts into a
  **self-calibrating floor** instead of a fixed number — EXP-004t measured
  that a floor learned from the live ambient wake stream (max observed
  ambient winner salience + 0.05) reaches the cost of hand-tuning a
  per-source gate, with zero detection loss (alarm-lane winners always
  bypass the floor, calibrated or not). Until 20 ambient samples have been
  observed it behaves exactly like `0.35` (today's old static default) —
  never more permissive while still learning. See
  `zugamind/act/floor_calibration.py`. State persists to
  `<data_dir>/floor_calibration.json`; a `floor_calibrated` journal event
  fires once, when the window fills.
