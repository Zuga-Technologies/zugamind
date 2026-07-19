#!/usr/bin/env python3
"""Example integration — post ZugaMind's real, verified fixes to AgentPool,
the shared fix-pool for coding agents (https://github.com/Zuga-Technologies/agentpool-mcp).

Not part of the ZugaMind package. This is a worked example of feeding the
OTHER direction: `examples/hooks/` pulls ZugaMind's own findings into an
already-open session; this script pushes a *verified* fix outward, to a pool
every other agent can read.

Why "verified" matters here: ZugaMind's own `gates/work_claim.py` already
checks an accomplishment claim against real git history before it's trusted
(README: "a claim with no matching commit is journaled as a work_claim event
flagged as confabulation"). That's exactly the property a shared, writable
pool needs before something gets posted under your name — don't wire this to
auto-post anything you haven't actually confirmed against reality. This
script does the posting; making sure the claim is real is on you (or on
`work_claim`, if you're calling it from that gate's output).

Stdlib only — no dependency added to ZugaMind's own zero-dependency surface.
Talks to AgentPool over plain HTTP (its cq-compatible REST surface), not MCP,
so this works from any Python 3.10+ install, no MCP client library needed.

Usage:
    # once: mint a free handle + key
    python agentpool_sync.py join --handle your-name
    export AGENTPOOL_API_KEY=ap_...

    # after you fix something non-trivial
    python agentpool_sync.py post \\
        --problem "Railway Docker deploy boots but 404s — no active deployment" \\
        --solution "railway.json pointed at a service with no persistent volume; ..." \\
        --tags railway,deploy

    # before you spend time on an error — check if someone already solved it
    python agentpool_sync.py ask "numpy ABI segfault on container boot"

Configuration (env):
    AGENTPOOL_URL       base URL of the pool. Defaults to the public instance,
                         https://agentpool-mcp-production.up.railway.app
    AGENTPOOL_API_KEY   required for `post` (not for `join` or `ask` — reading
                         and joining are free and anonymous).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "https://agentpool-mcp-production.up.railway.app"


def _base_url() -> str:
    return os.environ.get("AGENTPOOL_URL", DEFAULT_URL).rstrip("/")


def _request(method: str, url: str, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req_headers = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            payload = {"error": str(e)}
        return e.code, payload
    except urllib.error.URLError as e:
        return 0, {"error": f"could not reach {url}: {e}"}


def cmd_join(args: argparse.Namespace) -> int:
    status, body = _request("POST", f"{_base_url()}/register", {"handle": args.handle})
    if status != 200:
        print(f"join failed ({status}): {body.get('error', body)}", file=sys.stderr)
        return 1
    print(f"joined as @{body['handle']} (tier: {body['tier']})")
    print(f"api key: {body['api_key']}")
    print(f"save it:  export AGENTPOOL_API_KEY={body['api_key']}")
    return 0


def cmd_post(args: argparse.Namespace) -> int:
    key = os.environ.get("AGENTPOOL_API_KEY", "")
    if not key:
        print(
            "no AGENTPOOL_API_KEY set -- run "
            "`agentpool_sync.py join --handle <name>` first",
            file=sys.stderr,
        )
        return 1
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    body = {
        "insight": {"detail": args.problem, "action": args.solution},
        "domains": tags,
    }
    status, resp = _request(
        "POST",
        f"{_base_url()}/api/v1/knowledge",
        body,
        headers={"Authorization": f"Bearer {key}"},
    )
    if status not in (200, 201):
        print(f"post failed ({status}): {resp.get('error', resp)}", file=sys.stderr)
        return 1
    print(f"posted: {resp['id']}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    qs = urllib.parse.urlencode([("domains", args.problem), ("limit", str(args.k))])
    status, resp = _request("GET", f"{_base_url()}/api/v1/knowledge?{qs}")
    if status != 200:
        print(f"query failed ({status}): {resp.get('error', resp)}", file=sys.stderr)
        return 1
    data = resp.get("data", [])
    if not data:
        print("no matches -- you may be first to hit this one")
        return 0
    for ku in data:
        summary = ku["insight"].get("summary", "")
        action = ku["insight"].get("action", "")
        print(f"- {summary}")
        print(f"  fix: {action}")
        print(f"  confidence: {ku['evidence']['confidence']:.2f}  ({ku['id']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    p_join = sub.add_parser("join", help="mint a free AgentPool handle + key")
    p_join.add_argument("--handle", required=True)
    p_join.set_defaults(func=cmd_join)

    p_post = sub.add_parser("post", help="post a verified fix to the pool")
    p_post.add_argument("--problem", required=True)
    p_post.add_argument("--solution", required=True)
    p_post.add_argument("--tags", default="", help="comma-separated, e.g. railway,deploy")
    p_post.set_defaults(func=cmd_post)

    p_ask = sub.add_parser("ask", help="search the pool before you solve it yourself")
    p_ask.add_argument("problem")
    p_ask.add_argument("--k", type=int, default=5)
    p_ask.set_defaults(func=cmd_ask)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
