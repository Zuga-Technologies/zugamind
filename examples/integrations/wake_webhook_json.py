#!/usr/bin/env python3
"""Example integration — wake an n8n / Zapier / Make workflow with a JSON envelope.

The generic-webhook harness config POSTs the raw markdown briefing, which is
fine for an endpoint you wrote yourself — but workflow tools want STRUCTURED
fields they can map, filter, and route on ("only page me if salience > 0.8",
"route repo_issues wakes to the dev channel"). This script is the harness
command for that: it wraps the briefing in a small JSON envelope and POSTs
it, exiting nonzero on any failure so the engine's journal records a failed
wake as failed (the engine judges purely by exit code).

Envelope:

    {
      "source": "zugamind",
      "version": 1,
      "ts": "<UTC ISO time of the POST>",
      "event": "wake",
      "winner": {                 # best-effort, null if unavailable
        "module": "...",          # workspace module that won the competition
        "salience": 0.83,
        "thought_type": "...",
        "trigger_count": 4
      },
      "briefing": "<full briefing markdown>"
    }

The harness command gets only the briefing file path from the engine, so
`winner` comes from reading the LAST cycle event in ZugaMind's own
journal.jsonl — the wake fires immediately after that cycle, so it is the
right record in practice, but it is read best-effort: any problem (journal
missing, rotated, unparseable) degrades to `"winner": null` rather than
failing the wake. Route on it, don't bet your life on it.

Use it from a harness config (see examples/harness-configs/README.md for
the surrounding fields — your URL belongs in YOUR copy of harness.json,
never in this repo):

    "command": ["python", "/path/to/wake_webhook_json.py",
                "--url", "https://your-n8n.example/webhook/zugamind-wake",
                "{briefing_file}"]

Configuration:
    --url / ZUGAMIND_WEBHOOK_URL   the endpoint (flag wins over env)
    --header "Name: value"          repeatable — e.g. n8n header auth or a
                                    shared-secret header your flow checks
    --timeout <sec>                 per-attempt HTTP timeout (default 25;
                                    keep total under the config's
                                    timeout_sec so failures are curl-style
                                    clean exits, not engine hard-kills)
    ZUGAMIND_DATA_DIR               where journal.jsonl lives (same var the
                                    rest of the package uses)

One retry on transient failure (network error or HTTP 5xx) — HTTP 4xx is
not retried: a malformed request won't fix itself. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PKG_DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR")
                      or Path(__file__).resolve().parent.parent.parent / "zugamind" / "data")
_JOURNAL = _PKG_DATA_DIR / "engine" / "journal.jsonl"

_TAIL_BYTES = 64 * 1024
_RETRY_DELAY_SEC = 2.0


def _last_cycle_winner() -> dict[str, Any] | None:
    """Best-effort: the winner of the journal's most recent cycle event."""
    try:
        size = _JOURNAL.stat().st_size
        with open(_JOURNAL, encoding="utf-8", errors="replace") as f:
            f.seek(max(0, size - _TAIL_BYTES))
            lines = f.read().splitlines()
        for line in reversed(lines):
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("kind") == "cycle" and isinstance(ev.get("winner"), dict):
                w = ev["winner"]
                return {
                    "module": w.get("source_module"),
                    "salience": w.get("salience"),
                    "thought_type": w.get("thought_type"),
                    "trigger_count": ev.get("trigger_count"),
                }
    except Exception:
        pass
    return None


def _post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("briefing_file", type=Path)
    parser.add_argument("--url", default=os.environ.get("ZUGAMIND_WEBHOOK_URL", ""))
    parser.add_argument("--header", action="append", default=[],
                        metavar='"Name: value"')
    parser.add_argument("--timeout", type=float, default=25.0)
    args = parser.parse_args(argv)

    if not args.url:
        print("wake_webhook_json: no --url and ZUGAMIND_WEBHOOK_URL unset", file=sys.stderr)
        return 1
    try:
        briefing = args.briefing_file.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"wake_webhook_json: cannot read briefing: {e}", file=sys.stderr)
        return 1

    headers: dict[str, str] = {}
    for h in args.header:
        name, sep, value = h.partition(":")
        if sep and name.strip():
            headers[name.strip()] = value.strip()

    envelope = {
        "source": "zugamind",
        "version": 1,
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "wake",
        "winner": _last_cycle_winner(),
        "briefing": briefing,
    }
    body = json.dumps(envelope).encode("utf-8")
    host = urllib.parse.urlsplit(args.url).netloc

    last_err = ""
    for attempt in (1, 2):
        try:
            status = _post(args.url, body, headers, args.timeout)
            winner = (envelope["winner"] or {}).get("module")
            print(f"posted wake to {host} (status {status}, winner={winner})")
            return 0
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code < 500:
                break  # 4xx: our request is wrong; retrying won't fix it
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = str(e)
        if attempt == 1:
            time.sleep(_RETRY_DELAY_SEC)

    print(f"wake_webhook_json: POST to {host} failed: {last_err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
