"""A stranger's NOUN is not a ship claim (hm-#622).

Three earlier fixes each blanked one noun after it had bought a paid wake (a
version token, a retrospective "release", a bare "API"). The shared event union
still holds pricing, changelog, "release notes", "migration guide", "breaking
change" and "rate limits". For a publisher that is not the vendor, the gate now
also needs a finite VERB in the subject clause, so the next noun cannot do it.
The vendor's own host and ai_labs keep the noun union unchanged.
"""

import pytest

from scanners.world import hackernews

_STRANGER = "https://news.example.com/some-post"


@pytest.mark.parametrize("title", [
    # API (the 2026-09-27 shape, with the API noun as the only evidence)
    "Building on the Claude API",
    "OpenAI API",
    # pricing
    "OpenAI pricing page",
    "Claude pricing explained",
    # changelog
    "Claude Code changelog",
    # release notes
    "OpenAI release notes",
    # the rest of the union that could do the same
    "OpenAI migration guide",
    "Anthropic breaking change list",
    "Claude rate limits explained",
])
def test_a_noun_alone_does_not_promote_from_a_stranger(title):
    assert hackernews._is_vendor_ship(title, _STRANGER) is False, title
    assert hackernews._is_vendor_ship(title) is False, title  # no URL: still a stranger


@pytest.mark.parametrize("title", [
    "OpenAI launches new pricing tiers",
    "OpenAI releases GPT-6",
    "OpenAI API pricing cut by half",
    "Claude API now available in Europe",
    "Anthropic is deprecating the Claude 2 API",
    "Claude permanently raising weekly limits by 25%",
    "OpenAI outage affecting the Codex endpoint",
])
def test_a_real_verb_still_promotes_from_a_stranger(title):
    assert hackernews._is_vendor_ship(title, _STRANGER) is True, title


@pytest.mark.parametrize("title,url", [
    ("OpenAI pricing", "https://openai.com/pricing"),
    ("Claude Code changelog", "https://claude.com/changelog"),
    ("Anthropic API release notes", "https://www.anthropic.com/release-notes"),
])
def test_the_vendors_own_host_keeps_the_noun_as_the_announcement(title, url):
    assert hackernews._is_vendor_ship(title, url) is True, title


def test_the_shared_union_is_untouched_so_ai_labs_keeps_its_nouns():
    from scanners.world import ai_labs

    for noun in ("pricing", "changelog", "release notes", "migration guide",
                 "breaking change", "rate limits", "API"):
        assert ai_labs._SHIP_EVENT_RE.search(f"Claude {noun}"), noun
