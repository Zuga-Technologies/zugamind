"""A docs page is not an announcement.

Regression for the 2026-09-28 wake: "Prompting Claude Opus 5.5"
(platform.claude.com/docs/en/build-with-claude/prompt-engineering/
prompting-claude-opus-5-5) took the PROMOTED lane (relevance 0.9) on the
version-only lane -- `_version_grammar()` matched "Opus 5.5" (the vendor
prefix alternation includes "opus" itself) and `_is_vendor_host()` said yes
because the URL sits on platform.claude.com. Neither half asks whether the
page is a ship post or a permanent how-to guide. The gate now vetoes the
version-only lane when the URL path looks like documentation; a real claim
(a ship verb, a pricing/limits change) still promotes from the same host.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "zugamind"))

from scanners.world import hackernews  # noqa: E402

_DOCS_URL = (
    "https://platform.claude.com/docs/en/build-with-claude/"
    "prompt-engineering/prompting-claude-opus-5-5"
)
_BLOG_URL = "https://www.anthropic.com/news/claude-opus-5-5"


def test_the_measured_title_no_longer_promotes():
    assert hackernews._is_vendor_ship(
        "Prompting Claude Opus 5.5", _DOCS_URL
    ) is False


def test_other_docs_paths_stay_ambient():
    for url in (
        "https://platform.claude.com/docs/en/agents/claude-opus-5-5",
        "https://docs.anthropic.com/en/docs/claude-opus-5-5-guide",
        "https://openai.com/documentation/gpt-6-astra",
    ):
        assert hackernews._is_vendor_ship(
            "Building with Claude Opus 5.5", url
        ) is False, url


def test_real_ship_post_on_same_host_still_promotes():
    assert hackernews._is_vendor_ship(
        "Claude Opus 5.5", _BLOG_URL
    ) is True


def test_real_claims_on_a_docs_host_still_promote():
    assert hackernews._is_vendor_ship(
        "Anthropic launches Claude Opus 5.5", _DOCS_URL
    ) is True


def test_is_docs_url_helper():
    assert hackernews._is_docs_url(_DOCS_URL) is True
    assert hackernews._is_docs_url(_BLOG_URL) is False
    assert hackernews._is_docs_url("") is False
