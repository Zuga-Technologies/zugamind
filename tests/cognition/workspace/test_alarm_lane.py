"""Alarm-lane selection tests (issue #8, from EXP-001).

EXP-001 traced 2 of condition A's 3 missed canaries to lottery starvation:
a critical alarm repeatedly losing the salience^power weighted-random draw
to hotter ambient modules until its re-emission window closed. The fix: a
bid carrying a critical-urgency trigger AND healthy post-modulation salience
wins deterministically. Bids damped below ALARM_MIN_SALIENCE by the
attention schema (habituation / diversity caps) stay in the lottery — the
lane must not defeat alarm-fatigue protection.
"""
import random

from cognition.workspace import (
    SalienceBid,
    ThoughtType,
    Workspace,
    WorkspaceModule,
)


class _TriggerBidModule(WorkspaceModule):
    def __init__(self, name, salience, urgency=None):
        super().__init__()
        self.name = name
        self._salience = salience
        self._urgency = urgency

    def generate_bid(self, context):
        triggers = []
        if self._urgency is not None:
            triggers = [{
                "type": "local_service_down",
                "detail": f"[{self.name}] simulated failure",
                "urgency": self._urgency,
            }]
        return SalienceBid(
            source_module=self.name,
            content=f"{self.name} bidding",
            salience=self._salience,
            thought_type=ThoughtType.INFRASTRUCTURE,
            context={"triggers": triggers},
        )


def _run_once(seed, modules):
    random.seed(seed)
    ws = Workspace()
    for m in modules:
        ws.register_module(m)
    content = ws.run_cycle({})
    return content.source_module if content else None


def test_critical_alarm_beats_hotter_ambient_bid_every_time():
    """A 0.65-salience critical alarm vs a 0.9 ambient bid: under the pure
    lottery the ambient bid wins most draws; the alarm lane must make the
    alarm win ALL of them."""
    for seed in range(30):
        winner = _run_once(seed, [
            _TriggerBidModule("alarm", 0.65, urgency=1.0),
            _TriggerBidModule("ambient", 0.9, urgency=None),
        ])
        assert winner == "alarm", f"seed {seed}: lottery starved the alarm"


def test_damped_critical_stays_in_the_lottery():
    """Below ALARM_MIN_SALIENCE the lane is closed: a habituated repeat alarm
    (salience 0.2) must NOT deterministically beat a 0.9 ambient bid."""
    winners = {
        _run_once(seed, [
            _TriggerBidModule("tired_alarm", 0.2, urgency=1.0),
            _TriggerBidModule("ambient", 0.9, urgency=None),
        ])
        for seed in range(30)
    }
    assert "ambient" in winners, "damped alarm bypassed alarm-fatigue protection"


def test_two_criticals_highest_salience_wins_deterministically():
    for seed in range(10):
        winner = _run_once(seed, [
            _TriggerBidModule("crit_low", 0.6, urgency=0.95),
            _TriggerBidModule("crit_high", 0.8, urgency=0.95),
        ])
        assert winner == "crit_high"


def test_overlapping_criticals_rotate_instead_of_starving():
    """Two overlapping criticals: the hotter one must NOT win every cycle
    (that re-starves the cooler alarm — the EXP-001 C02 regression). After
    the hot one is served, the cooler one gets the next turn."""
    random.seed(20260711)
    ws = Workspace()
    hot = _TriggerBidModule("crit_hot", 0.85, urgency=0.95)
    cool = _TriggerBidModule("crit_cool", 0.66, urgency=0.95)
    ws.register_module(hot)
    ws.register_module(cool)
    winners = [ws.run_cycle({}).source_module for _ in range(4)]
    assert winners[0] == "crit_hot"  # first contact: salience tie-break
    assert "crit_cool" in winners[:2], f"cool critical starved: {winners}"
    assert set(winners) == {"crit_hot", "crit_cool"}


def test_sub_threshold_urgency_is_not_critical():
    """urgency below ALARM_URGENCY never enters the lane."""
    winners = {
        _run_once(seed, [
            _TriggerBidModule("warmish", 0.55, urgency=0.5),
            _TriggerBidModule("ambient", 0.9, urgency=None),
        ])
        for seed in range(30)
    }
    assert "ambient" in winners
