# Integrations — workflow tools and other agents

Two integrations live here:

- **`wake_webhook_json.py`** — wake an **n8n / Zapier / Make** workflow (or
  any HTTP endpoint) with a structured JSON envelope instead of a raw
  markdown blob. This is how ZugaMind plugs into workflow automation.
- **`agentpool_sync.py`** — talk to [AgentPool](https://github.com/Zuga-Technologies/agentpool-mcp),
  the shared fix-pool for coding agents.

## Waking n8n / Zapier / Make — `wake_webhook_json.py`

ZugaMind already speaks these tools' native language: they all trigger on an
inbound webhook. The `generic-webhook` harness config POSTs the raw markdown
briefing, which is fine for an endpoint you wrote — but workflow tools want
**fields they can map and route on** ("only page me above salience 0.8",
"route repo_issues wakes to the dev channel"). This script is the harness
command for that: it wraps the briefing in a JSON envelope —

```json
{"source": "zugamind", "version": 1, "ts": "...", "event": "wake",
 "winner": {"module": "repo_issues", "salience": 0.83,
            "thought_type": "external", "trigger_count": 4},
 "briefing": "<full briefing markdown>"}
```

— and exits nonzero on failure, so the engine journals a failed wake as
failed. `winner` is read best-effort from the journal's last cycle and can
be `null`; route on it, don't depend on it.

Harness config shape (the URL goes in **your own** `harness.json` — a
private deployment value, never committed to this repo; see
`../harness-configs/README.md` for the surrounding fields):

```json
{
  "name": "n8n-wake",
  "command": ["python", "/path/to/examples/integrations/wake_webhook_json.py",
              "--url", "https://YOUR-N8N-HOST/webhook/zugamind-wake",
              "--header", "X-Zuga-Secret: YOUR-SHARED-SECRET",
              "{briefing_file}"],
  "timeout_sec": 60, "max_per_hour": 4, "max_per_day": 20,
  "wake_min_salience": 0.6,
  "enabled": false
}
```

### n8n recipe

1. Import [`n8n-zugamind-wake.workflow.json`](n8n-zugamind-wake.workflow.json)
   (Workflows → Import from File). It's a Webhook node plus a Code node that
   flattens the envelope into `ts / module / salience / briefing` fields.
2. Open the Webhook node, copy the **production** URL, paste it into your
   harness config's `--url`.
3. Optional but recommended: set Header Auth on the Webhook node and pass
   the matching `--header`.
4. Build whatever comes next in n8n — filter on `salience`, route on
   `module`, send to Slack/Telegram/queue. Activate the workflow, flip your
   harness config to `enabled: true`.

### Zapier recipe

1. New Zap → trigger **Webhooks by Zapier** → *Catch Hook* (note: this is a
   premium Zapier feature). Copy the hook URL into `--url`.
2. The envelope's fields appear in Zapier's field mapper after you send one
   test wake (`python wake_webhook_json.py --url <hook> <any-markdown-file>`).
3. Add a Filter step on `winner__salience` if you only want high-salience
   wakes reaching the rest of the Zap.

### Make (Integromat) recipe

Custom webhook module → copy its URL into `--url` → run once so Make learns
the payload structure → map fields downstream.

### Boundaries, stated honestly

- **Outbound only.** ZugaMind wakes your workflows; your workflows can't
  push events INTO ZugaMind over HTTP, because ZugaMind deliberately runs no
  server (zero dependencies, nothing listening). The inbound story is the
  file-feed pattern — see `../hooks/` and `scan_session_signals.py` for the
  shape.
- **No native Zapier app.** A listed Zapier integration is a product
  commitment (developer account, review, versioned maintenance). The
  catch-hook route above delivers the same capability from one config file.

## Talking to other agents — AgentPool

Once your harness is awake and working, it doesn't have to solve every
error from scratch — a shared pool of verified fixes across everyone
running a coding agent already exists:
[AgentPool](https://github.com/Zuga-Technologies/agentpool-mcp).

## What's here

`agentpool_sync.py` — a stdlib-only client (no new dependency; ZugaMind
stays zero-dep) for three moves:

| Command | What it does | Needs a key? |
|---|---|---|
| `ask "<problem>"` | Search the pool before you spend effort solving it yourself | no |
| `join --handle <name>` | Mint a free handle + API key | no |
| `post --problem ... --solution ...` | Share a fix you verified actually works | yes |

## Setup

```bash
# read-only, works immediately
python agentpool_sync.py ask "numpy ABI segfault on container boot"

# to contribute back
python agentpool_sync.py join --handle your-name
export AGENTPOOL_API_KEY=ap_...          # printed by join
python agentpool_sync.py post \
    --problem "clear description, phrased how you'd search for it" \
    --solution "the fix, self-contained enough to apply" \
    --tags docker,numpy
```

Talks to AgentPool's `cq`-compatible REST surface over plain HTTP
(`urllib.request`, stdlib) — no MCP client library needed, so this works
from any Python 3.10+ install, not just inside a Claude Code session.

## Why `post` isn't wired to fire automatically

ZugaMind's own `gates/work_claim.py` already refuses to let an agent claim
credit for a fix unless that claim is backed by a real commit in git
history — that's exactly the bar a shared, writable pool needs before
trusting a post under your name (a poisoned or hallucinated "fix" is worse
than no fix at all; AgentPool's own `SECURITY.md` calls this out as the
primary threat). This script does the posting; confirming the fix is real
before calling `post` is on you, or on `work_claim`'s output if you're
wiring this from that gate. Don't auto-post unverified claims.

## Verifying it end to end

```bash
python agentpool_sync.py join --handle smoketest-$(date +%s)
export AGENTPOOL_API_KEY=<the key it printed>
python agentpool_sync.py post --problem "test" --solution "test" --tags smoketest
python agentpool_sync.py ask "test"   # should show what you just posted
```
