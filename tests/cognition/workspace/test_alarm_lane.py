"""Alarm-lane selection tests (issue #8 from EXP-001; refractory semantics
from EXP-003).

EXP-001 traced 2 of condition A's 3 missed canaries to lottery starvation:
a critical alarm repeatedly losing the salience^power weighted-random draw
to hotter ambient modules until its re-emission window closed. The fix: a
bid carrying a critical-urgency trigger wins deterministically.

EXP-003 then measured the cost of the original alarm-fatigue guard (lane
required post-modulation salience >= ALARM_MIN_SALIENCE): a module dampened
for its NOISE had its first-arrival CRITICAL silenced too — domreal_recall
0.2. Fatigue is now keyed on the alarm class itself: a (module, trigger
type) that recently won the lane sits out ALARM_REFRACTORY_CYCLES; a
first-arrival critical enters the lane no matter how dampened its module is.
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


def test_first_arrival_damped_critical_still_wins_the_lane():
    """The EXP-003 domreal regression: a module dampened to 0.2 for its
    chatter raises a FIRST-ARRIVAL critical — the lane must take it anyway.
    Under the old ALARM_MIN_SALIENCE guard this lost to ambient ~always."""
    for seed in range(30):
        winner = _run_once(seed, [
            _TriggerBidModule("damped_alarm", 0.2, urgency=1.0),
            _TriggerBidModule("ambient", 0.9, urgency=None),
        ])
        assert winner == "damped_alarm", (
            f"seed {seed}: dampening silenced a first-arrival critical")


def test_served_alarm_sits_out_the_refractory_window():
    """Alarm fatigue, new semantics: after an alarm class wins the lane, the
    SAME class re-bidding goes back to the lottery for
    ALARM_REFRACTORY_CYCLES — a repeat alarm must not own the workspace."""
    random.seed(20260712)
    ws = Workspace()
    ws.register_module(_TriggerBidModule("repeat_alarm", 0.2, urgency=1.0))
    ws.register_module(_TriggerBidModule("ambient", 0.9, urgency=None))
    winners = [ws.run_cycle({}).source_module for _ in range(6)]
    assert winners[0] == "repeat_alarm", "first arrival must take the lane"
    assert "ambient" in winners[1:], (
        f"served alarm kept winning through its refractory window: {winners}")


def test_alarm_class_regains_lane_after_refractory_expires():
    """A NEW incident from the same class after the window must be lane-
    eligible again (the window is fatigue, not a permanent mute)."""
    random.seed(20260712)
    ws = Workspace()
    alarm = _TriggerBidModule("cyclic_alarm", 0.2, urgency=1.0)
    ws.register_module(alarm)
    ws.register_module(_TriggerBidModule("ambient", 0.9, urgency=None))
    first = ws.run_cycle({}).source_module
    for _ in range(Workspace.ALARM_REFRACTORY_CYCLES):
        ws.run_cycle({})
    later = ws.run_cycle({}).source_module
    assert first == "cyclic_alarm"
    assert later == "cyclic_alarm", "alarm class never regained lane eligibility"


def test_lane_winner_is_flagged_for_downstream_wake_filters():
    """The lane's rescue must survive the harness salience floor: winners
    carry context['alarm_lane'] and stream.runner honors it (EXP-003:
    DOMREAL won selection at 0.15 salience and died at the 0.35 floor)."""
    from stream.runner import StreamRunner

    random.seed(20260712)
    ws = Workspace()
    ws.register_module(_TriggerBidModule("damped_alarm", 0.15, urgency=1.0))
    content = ws.run_cycle({})
    assert content.bid.context.get("alarm_lane") is True
    winner_dict = content.to_dict()
    hc = {"name": "t", "wake_min_salience": 0.35}
    assert StreamRunner._harness_wants(hc, winner_dict) is True
    # A non-lane winner below the floor is still filtered.
    assert StreamRunner._harness_wants(
        hc, {"source_module": "x", "salience": 0.15, "context": {}}) is False


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


def test_attention_health_disabled_bypasses_alarm_lane():
    """EXP-003 ablation switch: with attention_health_enabled=False, a
    critical alarm that would otherwise win every draw (see
    test_critical_alarm_beats_hotter_ambient_bid_every_time) is back in the
    plain weighted lottery — the ambient bid must win at least once."""
    winners = set()
    for seed in range(30):
        random.seed(seed)
        ws = Workspace(attention_health_enabled=False)
        ws.register_module(_TriggerBidModule("alarm", 0.65, urgency=1.0))
        ws.register_module(_TriggerBidModule("ambient", 0.9, urgency=None))
        content = ws.run_cycle({})
        winners.add(content.source_module if content else None)
    assert "ambient" in winners, "alarm lane still active despite attention_health_enabled=False"


def test_attention_health_disabled_bypasses_diversity_cap():
    """Same switch, soft-modulation side: a module winning every cycle
    should normally get dampened by streak/diversity corrections. With the
    switch off, raw salience alone decides — the same module keeps winning
    despite a repeated streak."""
    random.seed(20260711)
    ws = Workspace(attention_health_enabled=False)
    dominant = _TriggerBidModule("dominant", 0.9, urgency=None)
    quiet = _TriggerBidModule("quiet", 0.3, urgency=None)
    ws.register_module(dominant)
    ws.register_module(quiet)
    winners = [ws.run_cycle({}).source_module for _ in range(6)]
    assert winners.count("dominant") == 6, (
        f"diversity/streak correction still active despite the switch: {winners}"
    )
