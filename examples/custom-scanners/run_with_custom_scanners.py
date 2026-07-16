"""Example launcher — wiring your own scanners in.

The minimal shape of what examples/custom-scanners/README.md describes:
your own scan_* functions, injected via extra_scanners, no package changes.
Copy this alongside slack_mentions.py / jira_assigned.py (or your own
scanners following the same contract) and run it directly:

    python run_with_custom_scanners.py            # daemon, default 420s interval
    python run_with_custom_scanners.py --once      # one cycle, for testing

Set whichever env vars your chosen scanner(s) need (see each file's
docstring) before running — an unconfigured scanner returns [] rather than
erroring, so it's safe to wire in scanners you haven't finished configuring
yet.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Point this at wherever you've cloned zugamind — adjust for your layout.
ZUGAMIND_PKG = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ZUGAMIND_PKG))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stream.runner import StreamRunner  # noqa: E402

from jira_assigned import scan_jira_assigned  # noqa: E402
from slack_mentions import scan_slack_mentions  # noqa: E402


def main() -> int:
    runner = StreamRunner(extra_scanners={
        "scan_slack_mentions": scan_slack_mentions,
        "scan_jira_assigned": scan_jira_assigned,
    })

    if "--once" in sys.argv:
        result = runner.run_once()
        winner = result["winner"]
        summary = f"{winner['source_module']}: {winner['content']}" if winner else "(no winner)"
        print(f"state={result['state']} triggers={result['trigger_count']} winner={summary}")
        return 0

    runner.run_daemon(interval=420)
    return 0


if __name__ == "__main__":
    sys.exit(main())
