"""Core Workspace engine tests: bidding, winner selection, broadcast,
reportability (get_stats), and the pluggable modulator extension point.
"""
from cognition.workspace import (
    Workspace,
    ThoughtType,
    SalienceBid,
    WorkspaceContent,
    WorkspaceModule,
)


class _FixedBidModule(WorkspaceModule):
    def __init__(self, name, salience, thought_type=ThoughtType.KNOWLEDGE):
        super().__init__()
        self.name = name
        self._salience = salience
        self._thought_type = thought_type
        self.broadcasts_seen = []

    def generate_bid(self, context):
        return SalienceBid(
            source_module=self.name,
            content=f"{self.name} bidding",
            salience=self._salience,
            thought_type=self._thought_type,
        )

    def on_broadcast(self, content: WorkspaceContent):
        self.broadcasts_seen.append(content.source_module)


def test_salience_bid_validity():
    valid = SalienceBid("m", "content", 0.5, ThoughtType.KNOWLEDGE)
    assert valid.is_valid
    assert not SalienceBid("m", "", 0.5, ThoughtType.KNOWLEDGE).is_valid  # empty content
    assert not SalienceBid("m", "x", 1.5, ThoughtType.KNOWLEDGE).is_valid  # out of range


def test_no_bids_returns_none():
    ws = Workspace()
    assert ws.run_cycle({}) is None


def test_highest_salience_almost_always_wins():
    """Deterministic at selection_power's extreme: a 0.99 vs 0.01 bid should
    win overwhelmingly across many trials (salience**4 weighting)."""
    wins = 0
    trials = 40
    for _ in range(trials):
        ws = Workspace()
        high = _FixedBidModule("high", 0.95)
        low = _FixedBidModule("low", 0.05)
        ws.register_module(high)
        ws.register_module(low)
        content = ws.run_cycle({})
        if content.source_module == "high":
            wins += 1
    assert wins >= trials * 0.9


def test_winner_is_broadcast_to_all_modules():
    ws = Workspace()
    a = _FixedBidModule("a", 0.9)
    b = _FixedBidModule("b", 0.1)
    ws.register_module(a)
    ws.register_module(b)
    ws.run_cycle({})
    assert a.broadcasts_seen == ["a"]
    assert b.broadcasts_seen == ["a"]  # b sees the winner too, even though it lost


def test_get_stats_shape_is_reportable():
    ws = Workspace()
    ws.register_module(_FixedBidModule("solo", 0.5))
    ws.run_cycle({})
    stats = ws.get_stats()
    assert stats["cycle_count"] == 1
    assert stats["registered_modules"] == ["solo"]
    assert stats["current_content"]["source_module"] == "solo"
    assert stats["last_bids"][0]["module"] == "solo"
    assert "attention_schema" in stats


def test_module_bid_exception_does_not_break_cycle():
    class _Broken(WorkspaceModule):
        name = "broken"

        def generate_bid(self, context):
            raise RuntimeError("boom")

    ws = Workspace()
    ws.register_module(_Broken())
    ws.register_module(_FixedBidModule("ok", 0.5))
    content = ws.run_cycle({})
    assert content.source_module == "ok"


def test_registered_modulator_reweights_before_attention_schema():
    def boost_low(bids, context):
        for b in bids:
            if b.source_module == "low":
                b.salience = 0.99
        return bids

    ws = Workspace()
    ws.register_modulator(boost_low)
    ws.register_module(_FixedBidModule("low", 0.05))
    ws.register_module(_FixedBidModule("high", 0.5))
    content = ws.run_cycle({})
    assert content.source_module == "low"  # modulator ran before selection


def test_runner_up_is_recorded():
    ws = Workspace()
    ws.register_module(_FixedBidModule("first", 0.9))
    ws.register_module(_FixedBidModule("second", 0.5))
    content = ws.run_cycle({})
    assert content.runner_up is not None
    assert content.runner_up.source_module == "second"
