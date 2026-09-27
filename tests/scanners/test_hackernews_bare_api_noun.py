"""A product NOUN is not a ship claim.

Regression for the 2026-09-27 04:02Z paid wake: "OpenAI agents tried to
bruteforce a UN website's API fields" took the PROMOTED lane (relevance 0.9,
bid 0.67) on a stranger's blog, because `_SHIP_EVENT_RE`'s `\bapis?\b` matched
"API" as a plain noun. The gate now masks bare API/SDK before asking the ship
grammar; a title still promotes on any real claim it carries.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "zugamind"))

from scanners.world import hackernews  # noqa: E402

_STRANGER = "https://swarmcha.se/posts/openai-unctad"


def test_the_measured_title_no_longer_promotes():
    assert hackernews._is_vendor_ship(
        "OpenAI agents tried to bruteforce a UN website's API fields",
        _STRANGER,
    ) is False


def test_bare_noun_titles_stay_ambient():
    for title in (
        "Building a Claude API wrapper in Rust",
        "The OpenAI SDK is a mess",
        "Gemini APIs and the state of multimodal",
    ):
        assert hackernews._is_vendor_ship(title, _STRANGER) is False, title


def test_real_claims_still_promote():
    for title in (
        "OpenAI launches a new Realtime API",
        "Claude API now available in Europe",
        "OpenAI API pricing cut by half",
        "Anthropic releases Python SDK 1.0",
    ):
        assert hackernews._is_vendor_ship(title, _STRANGER) is True, title


def test_mask_keeps_length():
    t = "OpenAI agents tried to bruteforce a UN website's API fields"
    masked = hackernews._mask_bare_product_nouns(t)
    assert "api" not in masked.lower().replace("openai", "")
    assert len(masked) == len(t)
