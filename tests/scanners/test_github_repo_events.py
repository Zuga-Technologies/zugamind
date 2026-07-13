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
