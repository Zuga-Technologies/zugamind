"""WorldSignalsModule: world scanner types finally route; volume can't shout.

Regression lock for the 2026-08-06 routing-gap fix: before this module, no
world scanner trigger type appeared in any TRIGGER_TYPES set, so every
external signal was silently dropped by route_triggers_to_modules() before
the bid pass — collected, then discarded, never once influencing attention.
"""
import importlib

from cognition.workspace import create_all_modules, route_triggers_to_modules
from cognition.workspace.workspace_modules import (
    TRIGGER_TYPE_TO_MODULE,
    WorldSignalsModule,
)


def _trigger(ttype="hackernews_story", relevance=0.5, urgency=0.2, detail="HN: thing"):
    return {
        "type": ttype,
        "detail": detail,
        "novelty": 0.6,
        "relevance": relevance,
        "urgency": urgency,
    }


def test_every_world_scanner_type_routes_somewhere():
    # The exact regression: these existed for months while routing nowhere.
    for ttype in ("hackernews_story", "reddit_ai_post", "ai_lab_research",
                  "repo_star_delta", "repo_fork", "repo_release",
                  "reach_web_update", "reach_search_result"):
        assert ttype in TRIGGER_TYPE_TO_MODULE, f"{ttype} is orphaned again"
        assert TRIGGER_TYPE_TO_MODULE[ttype] == "world_signals"


def test_world_trigger_reaches_module_and_bids():
    modules = create_all_modules()
    route_triggers_to_modules([_trigger(detail="Show HN: agent eyes")], modules)
    mod = next(m for m in modules if m.name == "world_signals")
    assert len(mod._triggers) == 1
    bid = mod.generate_bid({})
    assert bid is not None
    assert "Show HN: agent eyes" in bid.content


def test_quiet_world_means_no_bid_not_a_low_bid():
    assert WorldSignalsModule().generate_bid({}) is None


def test_salience_from_strongest_signal_not_volume():
    one = WorldSignalsModule()
    one.set_triggers([_trigger(relevance=0.9, urgency=0.5)])
    many = WorldSignalsModule()
    many.set_triggers([_trigger(relevance=0.2, urgency=0.1)] * 50)
    assert one.generate_bid({}).salience > many.generate_bid({}).salience
    # volume is reported honestly in the content, not amplified into salience
    assert "+49 more" in many.generate_bid({}).content


def test_salience_capped_below_ops_territory():
    mod = WorldSignalsModule()
    mod.set_triggers([_trigger(relevance=1.0, urgency=1.0)])
    assert mod.generate_bid({}).salience <= 0.75


def test_link_key_surfaces_as_top_url():
    # news_rss / ai_lab_research triggers carry their URL under "link", not
    # "url" — reading only "url" left top_url null in every journal cycle,
    # making those types invisible to downstream consumers (found 2026-08-06).
    mod = WorldSignalsModule()
    t = _trigger(ttype="ai_lab_research", relevance=0.8)
    t["link"] = "https://openai.com/research/thing"
    mod.set_triggers([t])
    assert mod.generate_bid({}).context["top_url"] == "https://openai.com/research/thing"


def test_extra_types_env_extends_routing(monkeypatch):
    monkeypatch.setenv("ZUGAMIND_WORLD_SIGNAL_EXTRA_TYPES",
                       "tester_crash, custom_signal")
    import cognition.workspace.workspace_modules as wm
    importlib.reload(wm)
    try:
        assert wm.TRIGGER_TYPE_TO_MODULE["tester_crash"] == "world_signals"
        assert wm.TRIGGER_TYPE_TO_MODULE["custom_signal"] == "world_signals"
    finally:
        monkeypatch.delenv("ZUGAMIND_WORLD_SIGNAL_EXTRA_TYPES")
        importlib.reload(wm)
