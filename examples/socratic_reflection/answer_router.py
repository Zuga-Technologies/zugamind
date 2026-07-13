"""Answer-source router — resolves a generated question to a concrete answer.

Given a question + answer_source_hint produced by question_generator.py,
dispatch to a concrete answer source and return a structured dict:

    {
        "source":     "code_search" | "file_read" | "none",
        "content":    <answer text, possibly truncated>,
        "success":    bool,
        "latency_ms": int,
        "meta":       dict
    }

Phase scope in this OSS release:
  - code_search: REAL — subprocess `git grep` (falls back to `grep`) over
    the local repo.
  - file_read:   stub, returns success=False (implement against your own
    knowledge store).
  - none:        null content.

The origin project also supported a "palace_walk" source (a structured
personal-knowledge-base walk) and a "scanner_replay" source (replaying
recent internal reflections from a private event-log schema) — both
depended on private backends not part of this release and are omitted.
Add your own answer sources by extending `answer_question` below.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_VALID_SOURCES = ("code_search", "file_read", "none")
_CONTENT_CAP = 20000

_STOPWORDS = frozenset({
    "the", "is", "a", "an", "what", "how", "why", "when", "where", "which",
    "does", "do", "did", "are", "to", "of", "for", "in", "on", "and", "or",
    "this", "that", "these", "those", "with", "as", "by", "from", "into",
    "workspace", "zugamind", "current", "state", "status", "recent",
})


def _extract_keywords(question: str, k: int = 3) -> list[str]:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", question)
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokens:
        low = tok.lower()
        if low in _STOPWORDS or low in seen:
            continue
        seen.add(low)
        out.append(tok)
        if len(out) >= k:
            break
    return out


def _answer_via_code_search(question: str) -> dict[str, Any]:
    keywords = _extract_keywords(question, k=3)
    if not keywords:
        return {"content": "no extractable keywords", "success": False, "meta": {"keywords": []}}

    pattern = "|".join(re.escape(kw) for kw in keywords)
    cmds = [
        ["git", "grep", "-n", "-I", "-E", "--max-count=3", pattern],
        ["grep", "-rn", "-I", "-E", "--exclude-dir=.git", "--exclude-dir=node_modules",
         "--exclude-dir=.venv", "--exclude-dir=__pycache__", pattern, "."],
    ]
    out = ""
    err_msg = ""
    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=8)
            if proc.returncode in (0, 1):  # 1 = no matches, still valid
                out = (proc.stdout or "").strip()
                break
            err_msg = (proc.stderr or "")[:200]
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            err_msg = str(exc)
            continue
    else:
        return {"content": f"code_search failed: {err_msg}", "success": False,
                "meta": {"keywords": keywords}}

    if not out:
        return {"content": f"no matches for `{', '.join(keywords)}`", "success": True,
                "meta": {"keywords": keywords, "matches": 0}}

    raw_lines = out.splitlines()[:20]
    by_file: dict[str, list[tuple[str, str]]] = {}
    for line in raw_lines:
        parts = line.split(":", 2)
        if len(parts) == 3:
            f, lineno, match = parts
            by_file.setdefault(f, []).append((lineno, match.strip()))
        else:
            by_file.setdefault("(misc)", []).append(("", line))

    md_parts = [f"Found {len(raw_lines)} matches for `{', '.join(keywords)}`:\n"]
    for f, hits in by_file.items():
        md_parts.append(f"**`{f}`**")
        md_parts.append("```")
        for lineno, match in hits:
            prefix = f"{lineno}: " if lineno else ""
            md_parts.append(f"{prefix}{match}")
        md_parts.append("```")
    content = "\n".join(md_parts)[:_CONTENT_CAP]
    return {"content": content, "success": True, "meta": {"keywords": keywords, "matches": len(raw_lines)}}


def answer_question(
    question_text: str,
    source_hint: str,
    *,
    meta: dict[str, Any] | None = None,
    trigger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Route a question to its hinted answer source. Never raises.

    `trigger` (optional): the trigger the question came from; unused by the
    stock sources here but threaded through so a custom source can use it.
    """
    if source_hint not in _VALID_SOURCES:
        source_hint = "none"

    t0 = time.monotonic()
    if source_hint == "code_search":
        r = _answer_via_code_search(question_text)
    elif source_hint == "file_read":
        r = {"content": "source 'file_read' not implemented — wire your own "
                         "knowledge store here", "success": False, "meta": {}}
    else:
        r = {"content": "", "success": False, "meta": {"reason": "no source attempted"}}

    return {
        "source": source_hint,
        "content": r["content"],
        "success": r["success"],
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "meta": r.get("meta", {}),
    }
