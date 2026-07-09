"""Work-claim gate — does a reflection's accomplishment claim have an artifact?

A grounding/specificity gate can ask "is this reflection SPECIFIC?" (digits,
paths, proper nouns) — necessary but not sufficient: "I've streamlined the
codebase to reduce latency" is specific AND a fabrication when no commit backs
it. A specificity gate passes it; this gate catches it.

Confabulation = past/progressive accomplishment claim with no real artifact.
The source of truth is a git commit in the window — an artifact that
unambiguously happened. Future plans ("I plan to fix X", "next: refactor Y")
are honest and never flagged.

Stdlib-only. Fail-OPEN: any probe error returns backed=True so reflection is
never wrongly blocked. Reflection > false-positive.
"""
from __future__ import annotations

import functools
import logging
import os
import re
import subprocess
from typing import List

logger = logging.getLogger(__name__)

# Verbs that, in past/present-progressive form, assert work was/is being done.
WORK_CLAIM_VERBS: tuple[str, ...] = (
    "streamlined", "streamlining", "integrated", "integrating",
    "implemented", "implementing", "fixed", "fixing", "shipped", "shipping",
    "refactored", "refactoring", "optimized", "optimizing", "built", "building",
    "deployed", "deploying", "wired", "wiring", "migrated", "migrating",
    "rewrote", "rewriting", "applied fixes", "applied a fix", "applying fixes",
    "added support", "removed", "patched", "patching", "completed", "finished",
    "set up", "setup", "set-up", "installed", "installing", "configured",
    "configuring", "upgraded", "upgrading", "created", "creating", "enabled",
    "enabling", "provisioned", "provisioning", "launched", "launching",
    "established", "adopted", "adopting", "explored", "exploring", "switched to",
    "moved to", "moving to", "rolled out", "rolling out", "set-up",
)

# Hedges → the sentence is a plan, not a claim of done work → honest, not flagged.
WORK_HEDGE_WORDS: tuple[str, ...] = (
    "plan to", "planning to", "will ", "i'll", "next step", "next:", "intend",
    "should ", "could ", "would ", "want to", "going to", "propose", "considering",
    "thinking about", "might ", "aim to", "hope to", "need to", "todo", "to-do",
)


# Tokens that carry no specific-work signal — excluded from claim<->commit
# matching so overlap keys on real content (a project/file/feature name), not
# boilerplate. Includes the claim verbs themselves and generic self-words.
_CLAIM_STOPWORDS: frozenset = frozenset(
    {v.split()[0] for v in WORK_CLAIM_VERBS}
    | {
        "code", "codebase", "system", "performance",
        "efficiency", "improvement", "improvements", "integration", "various",
        "into", "with", "after", "from", "this", "that", "have", "been",
        "their", "framework", "solutions", "alternative", "moving", "forward",
        "focus", "monitoring", "investigating", "understanding", "key", "open",
        "questions", "decisions", "while", "exploring", "enhance", "tools",
    }
)


def _repo_root() -> str | None:
    cur = os.path.dirname(os.path.abspath(__file__))
    for _ in range(12):
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _recent_commits(window_minutes: int, root: str) -> List[str]:
    """Commit subjects across ALL branches in the window (autonomous work may
    land on a non-checked-out branch)."""
    try:
        out = subprocess.run(
            ["git", "log", "--all", f"--since={window_minutes} minutes ago", "--pretty=%s"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return []
        return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    except Exception as e:
        logger.debug("work_claim git log failed: %s", e)
        return []


def _split_sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.!?\n])\s+", (text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def _recent_commit_corpus(window_minutes: int, root: str) -> str:
    """Subjects + changed file paths + DIFF BODIES for recent commits across all
    branches. A claim is verified against what commits ACTUALLY touched (the
    patch), not just their one-line subjects — so naming a file/identifier you
    did not change no longer passes on a shared topic word."""
    try:
        out = subprocess.run(
            ["git", "log", "--all", f"--since={window_minutes} minutes ago",
             "--pretty=format:%s", "-p", "--unified=0"],
            cwd=root, capture_output=True, text=True, timeout=15,
        )
        return out.stdout.lower() if out.returncode == 0 else ""
    except Exception as e:  # noqa: BLE001
        logger.debug("_recent_commit_corpus failed: %s", e)
        return ""


def check_work_claim(text: str, window_minutes: int = 30, commits: List[str] | None = None) -> dict:
    """Verify accomplishment claims in `text` against real artifacts.

    Returns {"backed": bool, "unbacked": [sentences], "reason": str,
             "commits": n}. backed=True when there are no unbacked claims
    (either no claims at all, or a commit exists in the window). Fail-open.

    `commits` may be injected (tests) to skip the live git probe.
    """
    try:
        sentences = _split_sentences(text)
        claims = []
        for s in sentences:
            low = s.lower()
            if any(h in low for h in WORK_HEDGE_WORDS):
                continue  # future plan — honest
            if any(v in low for v in WORK_CLAIM_VERBS):
                claims.append(s)

        if not claims:
            return {"backed": True, "unbacked": [], "reason": "no_work_claim", "commits": 0}

        if commits is None:
            root = _repo_root()
            commits = _recent_commits(window_minutes, root) if root else []
            corpus = _recent_commit_corpus(window_minutes, root) if root else ""
        else:
            corpus = " ".join(commits).lower()  # injected (tests): no diff available
        if not corpus:
            corpus = " ".join(commits).lower()

        # Diff-aware backing. A claim is backed against the recent-commit
        # CORPUS = subjects + changed file paths + diff bodies, not just
        # subjects. SPECIFIC tokens (file/path/identifier — contain . / or _)
        # that the claim names MUST appear in that corpus: you cannot claim
        # work on a file/module a commit did not actually touch. Generic
        # (prose) tokens still match the corpus as a weaker fallback.
        subjects_blob = " ".join(commits).lower()  # tight: one-line subjects only
        unbacked = []
        for s in claims:
            toks = [w for w in (t.strip("._+-")
                    for t in re.findall(r"[a-z][a-z0-9._+-]{3,}", s.lower()))
                    if len(w) >= 4 and w not in _CLAIM_STOPWORDS]
            # "specific" = a code artifact (path/file/snake_case): INTERNAL . / or _
            # after punctuation strip, so a trailing-period word isn't mistaken for one.
            specific = [t for t in toks if any(c in t for c in "._/")]
            if specific:
                # Named a file/identifier -> it MUST appear in a real commit diff.
                if not any(t in corpus for t in specific):
                    unbacked.append(s)
            elif not any(t in subjects_blob for t in toks):
                # Prose claim -> match commit SUBJECTS only (not the noisy diff body),
                # so a topic word does not pass just by appearing somewhere in a patch.
                unbacked.append(s)

        if not unbacked:
            return {"backed": True, "unbacked": [], "reason": "artifact_matched",
                    "commits": len(commits)}

        return {
            "backed": False,
            "unbacked": unbacked[:3],
            "reason": f"work_claim_no_matching_commit ({len(unbacked)} unbacked, {len(commits)} commits/{window_minutes}min)",
            "commits": len(commits),
        }
    except Exception as e:
        logger.debug("check_work_claim failed (fail-open): %s", e)
        return {"backed": True, "unbacked": [], "reason": "gate_error", "commits": 0}


# =============================================================================
# Entity grounding — catch fabricated NAMES regardless of verb phrasing.
#
# A verb allowlist is unbounded-leaky ("ClickHouse is now in our stack" has no
# listed verb). This checks the NOUN instead: if a reflection names a
# tool/system/project (ClickHouse, JanusMesh, Homebrew) that appears NOWHERE in
# the codebase or commits, the claim is about something that doesn't exist here
# -> confabulation, regardless of how it's worded. Stdlib-only, fail-open.
# =============================================================================

# Capitalized/CamelCase/version-tagged proper nouns that are NOT project-specific
# entities — sentence-start words, self-terms, and generic tech we don't want to
# treat as "must exist in repo".
_ENTITY_STOPWORDS: frozenset = frozenset({
    # Cognitive state names are core vocabulary, not fabricated entities. Without
    # these, a DREAMING-state post would be suppressed as "ungrounded entity:
    # DREAMING" (others passed only by appearing incidentally in code).
    "dreaming", "resting", "curious", "focused", "alert", "reflecting",

    "i", "i've", "the", "a", "an", "my", "our", "next", "key", "open", "moving",
    "forward", "additionally", "also", "today", "yesterday",
    "claude", "discord", "python", "homebrew",
    "ollama", "github", "anthropic",
    "openai", "decisions", "questions", "framework", "focus", "goal",
    # state labels + section headers + common emphasis/sentence-start
    # words that a curated stoplist needs beyond a system dictionary:
    "fact", "take",
    "keep", "analyze", "considering", "investigating", "exploring",
    "understanding", "troubleshooting", "addressing", "high", "low", "wip",
})

# Any capitalized/all-caps token (>=3 chars). Case-shape does NOT decide — a
# real name is one that ISN'T a common dictionary word. So "ClickHouse",
# "Postgres", "Homebrew" survive; "Analyze", "Focused", "Fact", "Take", "Keep"
# are dropped because they're ordinary words. The dictionary filter does the
# work; the curated stoplist is the cross-platform fallback (Windows has no
# /usr/share/dict/words).
_ENTITY_RE = re.compile(r"\b([A-Z][a-zA-Z][a-zA-Z0-9]{2,}(?:\.[a-z0-9]+)?)\b")


@functools.lru_cache(maxsize=1)
def _common_words() -> frozenset:
    """Lowercased system dictionary. Empty on machines without one."""
    for p in ("/usr/share/dict/words", "/usr/dict/words"):
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                return frozenset(w.strip().lower() for w in f if w.strip())
        except Exception:
            continue
    return frozenset()


def _extract_entities(text: str) -> List[str]:
    common = _common_words()
    out, seen = [], set()
    for m in _ENTITY_RE.finditer(text or ""):
        e = m.group(1)
        el = e.lower()
        if el in _ENTITY_STOPWORDS or el in common:
            continue  # ordinary word, not a name
        if el not in seen:
            seen.add(el)
            out.append(e)
    return out


# Dependency manifests where an external tool would appear if REALLY integrated.
_DEP_FILES = ("*requirements*.txt", "pyproject.toml", "*package.json",
              "Brewfile", "*.cfg", "*.toml", "*.lock")


@functools.lru_cache(maxsize=512)
def _entity_in_repo(entity_lower: str, root: str) -> bool:
    """True iff the entity is actually INTEGRATED, not merely mentioned.

    A prose mention (comment, diary, docstring, an example in this gate) is NOT
    grounding — that is how full-text grep would false-ground 'ClickHouse'. Real
    integration shows up as one of:
      - a symbol definition:  `class X` / `def x`
      - an import:            `import x` / `from x`
      - a dependency manifest entry (requirements/pyproject/package.json/Brewfile)
    Prose never matches these patterns, so comments/diaries can't self-ground.
    """
    try:
        e = re.escape(entity_lower)
        code_pat = rf"(class|def|import|from)[^\n]*\b{e}\b"
        if subprocess.run(["git", "grep", "-i", "-q", "-E", code_pat],
                          cwd=root, capture_output=True, timeout=8).returncode == 0:
            return True
        if subprocess.run(["git", "grep", "-i", "-q", "-w", entity_lower, "--", *_DEP_FILES],
                          cwd=root, capture_output=True, timeout=8).returncode == 0:
            return True
        return False
    except Exception:
        return True  # fail-open: can't check -> don't flag


# External-reference markers. An entity in a sentence with one of these is being
# DISCUSSED (a mention), not claimed as adopted — so it must NOT be gated as an
# ungrounded build-claim. Keeps "I read about ClickHouse on Reddit" honest while
# still catching the verb-free confab "ClickHouse is now in our stack" (no marker).
_MENTION_MARKERS: tuple = (
    "read ", "reading", "about ", "reddit", "hacker news", "hackernews", " hn ",
    "thread", " post ", "article", "paper", "arxiv", "blog", "tweet", "twitter",
    "compar", "versus", " vs ", " vs.", "saw ", "noticed", "according to",
    "says", "said", "their ", "external", "competitor", "launch", "announced",
    "interesting", "story", "news", "report", "caught my attention", "what ",
)


def _is_mention(sentence_lower: str) -> bool:
    return any(m in sentence_lower for m in _MENTION_MARKERS)


def check_entity_grounding(text: str, commits: List[str] | None = None) -> dict:
    """Flag named entities that are used nowhere in the real codebase.

    `commits` is accepted for signature parity but intentionally NOT used as a
    grounding source — a commit message describing a confab must not ground it.
    Returns {"grounded": bool, "ungrounded": [names], "reason": str}. Fail-open.
    """
    try:
        # Hedge-aware: only flag entities asserted as DONE/owned. A hedged or
        # forward-looking sentence ("considering Postgres", "I plan to evaluate
        # ClickHouse") is honest exploration, not a claim — skip its entities.
        # Keep only CLAIM sentences: drop hedged/forward-looking ones AND
        # mention sentences (external references). Entities are gated only when
        # asserted as the agent's own, not when merely discussed.
        asserted = " ".join(
            s for s in _split_sentences(text)
            if not any(h in s.lower() for h in WORK_HEDGE_WORDS)
            and not _is_mention(s.lower())
        )
        entities = _extract_entities(asserted)
        if not entities:
            return {"grounded": True, "ungrounded": [], "reason": "no_entities"}

        root = _repo_root()
        if not root:
            return {"grounded": True, "ungrounded": [], "reason": "no_repo"}

        ungrounded = []
        for e in entities:
            if _entity_in_repo(e.lower(), root):
                continue
            ungrounded.append(e)

        if not ungrounded:
            return {"grounded": True, "ungrounded": [], "reason": "entities_in_repo"}
        return {
            "grounded": False,
            "ungrounded": ungrounded[:5],
            "reason": f"ungrounded_entities ({len(ungrounded)} not in codebase/commits)",
        }
    except Exception as e:
        logger.debug("check_entity_grounding failed (fail-open): %s", e)
        return {"grounded": True, "ungrounded": [], "reason": "gate_error"}


def gate_human_claim(text: str) -> tuple:
    """Reusable backstop for HUMAN-facing channels (chatroom/DM/inbox) — the
    paths a central send-message chokepoint does NOT cover. Returns
    (allowed, reason). Suppresses an unbacked work-claim or an ungrounded
    (fabricated) entity. Fail-OPEN: any error -> allowed=True, so it never
    blocks real comms."""
    try:
        t = text or ""
        if any(v in t.lower() for v in WORK_CLAIM_VERBS):
            wc = check_work_claim(t)
            if not wc.get("backed", True):
                return False, "unbacked work-claim: " + str(wc.get("reason", ""))
        eg = check_entity_grounding(t)
        if not eg.get("grounded", True):
            return False, "ungrounded entity: " + ", ".join(eg.get("ungrounded", []))
        return True, "ok"
    except Exception as e:  # noqa: BLE001 — fail-open
        return True, "gate-error-failopen: " + str(e)
