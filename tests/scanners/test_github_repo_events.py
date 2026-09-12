"""Tests for the repo-events scanner's pure diff core (no network)."""
import unittest

from scanners.world.github_repo_events import diff_state, _crossed_milestone


def _state(stars=0, forks=0, release_id=None, tag=""):
    return {"stars": stars, "forks": forks, "release_id": release_id,
            "release_tag": tag}


class DiffStateTest(unittest.TestCase):
    def test_first_observation_baselines_silently(self):
        # A fresh deployment must not fire a fake "+N stars" trigger.
        self.assertEqual(diff_state("o/r", None, _state(stars=10)), [])
        self.assertEqual(diff_state("o/r", {}, _state(stars=10)), [])

    def test_star_gain_emits_delta(self):
        (t,) = diff_state("o/r", _state(stars=8), _state(stars=9))
        self.assertEqual(t["type"], "repo_star_delta")
        self.assertIn("gained 1 star", t["detail"])
        self.assertEqual(t["urgency"], 0.4)  # routine gain, not milestone

    def test_milestone_crossing_boosts_urgency(self):
        (t,) = diff_state("o/r", _state(stars=9), _state(stars=10))
        self.assertIn("crossed 10 stars", t["detail"])
        self.assertEqual(t["urgency"], 0.6)

    def test_star_loss_is_silent(self):
        self.assertEqual(diff_state("o/r", _state(stars=10), _state(stars=9)), [])

    def test_fork_and_release_emit(self):
        prev = _state(stars=10, forks=0, release_id=1, tag="v0.1.0")
        cur = _state(stars=10, forks=1, release_id=2, tag="v0.2.0")
        types = {t["type"] for t in diff_state("o/r", prev, cur)}
        self.assertEqual(types, {"repo_fork", "repo_release"})

    def test_release_by_us_is_priced_as_echo_not_news(self):
        # 2026-09-12: two paid wakes fired on Ludus releases THIS machine had
        # published minutes earlier. The author is the only field that tells
        # our own ship apart from a teammate's.
        prev = _state(release_id=1, tag="v0.15.25")
        cur = dict(_state(release_id=2, tag="v0.15.26"), release_author="Zuga-luga")
        (t,) = diff_state("Zuga-Technologies/Ludus", prev, cur)
        self.assertEqual(t["type"], "repo_release")
        self.assertTrue(t["self_authored"])
        self.assertLess(t["relevance"], 0.5)
        self.assertLess(t["urgency"], 0.2)
        self.assertIn("echo", t["detail"])

    def test_release_by_teammate_keeps_full_weight(self):
        prev = _state(release_id=1, tag="v0.1.0")
        cur = dict(_state(release_id=2, tag="v0.2.0"), release_author="mike-somebody")
        (t,) = diff_state("o/r", prev, cur)
        self.assertFalse(t["self_authored"])
        self.assertEqual(t["relevance"], 0.8)
        self.assertIn("mike-somebody", t["detail"])

    def test_release_with_unknown_author_keeps_full_weight(self):
        # Legacy cache entries have no author field: never discount blind.
        (t,) = diff_state("o/r", _state(release_id=1), _state(release_id=2, tag="v2"))
        self.assertFalse(t["self_authored"])
        self.assertEqual(t["relevance"], 0.8)

    def test_unchanged_state_emits_nothing(self):
        s = _state(stars=10, forks=2, release_id=5)
        self.assertEqual(diff_state("o/r", s, dict(s)), [])

    def test_trigger_ids_are_stable_dedupe_keys(self):
        # Same end-state must produce the same id, so a re-emit after a cache
        # rollback still dedupes in habituation.
        (a,) = diff_state("o/r", _state(stars=8), _state(stars=9))
        (b,) = diff_state("o/r", _state(stars=8), _state(stars=9))
        self.assertEqual(a["id"], b["id"])

    def test_milestone_helper(self):
        self.assertEqual(_crossed_milestone(9, 10), 10)
        self.assertEqual(_crossed_milestone(9, 120), 100)  # highest crossed
        self.assertIsNone(_crossed_milestone(10, 11))
        self.assertIsNone(_crossed_milestone(10, 10))


if __name__ == "__main__":
    unittest.main()
