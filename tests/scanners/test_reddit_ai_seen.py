"""reddit_ai: a stickied post is not news twice.

/hot is not a queue of new things. Before the seen-set, engine habituation was
the only thing between a permanently-stickied post and the workspace, and it
forgets after HABITUATION_HOURS — so r/singularity's "Discord Server Link"
won the global workspace twelve times in nine days.
"""
from __future__ import annotations

import json
import time

import pytest

import scanners.world.reddit_ai as reddit_ai
from scanners import seen_items


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("ZUGAMIND_DATA_DIR", str(tmp_path))
    return tmp_path / "scanner_cache"


def _post(pid, title="A post", sub="singularity"):
    return {"id": pid, "title": title, "sub": sub,
            "link": f"https://reddit.invalid/{pid}"}


def _seed(cache, posts):
    reddit_ai._cache_path().write_text(json.dumps(posts), encoding="utf-8")


def test_cold_start_baselines_silently(cache):
    _seed(cache, [_post("a"), _post("b")])

    assert reddit_ai.scan_reddit_ai() == []
    assert len(json.loads((cache / "reddit_ai_seen.json").read_text())) == 2


def test_stickied_post_triggers_once_then_never(cache):
    sticky = _post("sticky", "Discord Server Link")
    _seed(cache, [sticky])
    reddit_ai.scan_reddit_ai()  # baseline

    _seed(cache, [sticky, _post("new", "An actual new post")])
    assert [t["detail"].split(": ", 1)[1] for t in reddit_ai.scan_reddit_ai()] \
        == ["An actual new post"]

    for _ in range(10):
        _seed(cache, [sticky, _post("new", "An actual new post")])
        assert reddit_ai.scan_reddit_ai() == []


def test_dark_feed_stays_cold(cache):
    _seed(cache, [])

    assert reddit_ai.scan_reddit_ai() == []
    assert not (cache / "reddit_ai_seen.json").exists()


def test_brand_mention_still_scores_above_ambient_chatter(cache, monkeypatch):
    """The seen-set must not flatten the brand lane — it only stops repeats."""
    monkeypatch.setenv("ZUGAMIND_BRAND_TERMS", "ZugaMind")
    _seed(cache, [_post("base")])
    reddit_ai.scan_reddit_ai()

    _seed(cache, [_post("base"),
                  _post("brand", "ZugaMind is interesting"),
                  _post("plain", "Something else entirely")])
    by_urgency = {t["detail"]: t for t in reddit_ai.scan_reddit_ai()}

    brand = next(t for d, t in by_urgency.items() if "BRAND MENTION" in d)
    plain = next(t for d, t in by_urgency.items() if "BRAND MENTION" not in d)
    assert brand["urgency"] > plain["urgency"]
    assert brand["relevance"] > plain["relevance"]


# --------------------------------------------------------------------------
# the shared helper
# --------------------------------------------------------------------------

def test_missing_and_corrupt_seen_files_both_read_as_cold_start(tmp_path):
    missing = tmp_path / "nope.json"
    assert seen_items.read_seen(missing) is None

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{ not json")
    assert seen_items.read_seen(corrupt) is None

    wrong_shape = tmp_path / "list.json"
    wrong_shape.write_text("[1, 2, 3]")
    assert seen_items.read_seen(wrong_shape) is None


def test_empty_seen_file_is_not_a_cold_start(tmp_path):
    """{} means "we have looked and seen nothing" — None means "no idea".
    Collapsing the two would re-baseline, and a baseline emits nothing."""
    path = tmp_path / "empty.json"
    path.write_text("{}")
    assert seen_items.read_seen(path) == {}


def test_prune_keeps_newest_not_alphabetically_last(tmp_path):
    path = tmp_path / "seen.json"
    now = time.time()
    seen_items.write_seen(path, {"zzz-oldest": now - 100, "aaa-newest": now,
                                 "mmm-middle": now - 50}, max_keys=2)

    assert set(json.loads(path.read_text())) == {"aaa-newest", "mmm-middle"}


def test_write_creates_missing_parent_directory(tmp_path):
    path = tmp_path / "deep" / "nested" / "seen.json"
    seen_items.write_seen(path, {"k": time.time()}, max_keys=10)
    assert path.exists()
