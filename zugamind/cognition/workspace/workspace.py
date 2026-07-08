"""
ZugaMind Workspace — Global Workspace Theory for autonomous agents.

Stdlib-only synchronous implementation of the GWT pattern: modules submit
salience bids, an attention schema modulates them, one winner is selected
and broadcast to all modules. This is the engineered, steerable, fully-logged
"admission" mechanism ZugaMind implements explicitly — the thing Anthropic's
"A global workspace in language models" paper (2026-07-06) names as still
unknown for the model's internal workspace.

Cycle: gather bids -> apply registered modulators -> attention schema
modulates -> hard diversity ceiling -> select winner (salience^power
weighted random) -> broadcast -> attention schema updates its self-model.

Zero pip dependencies (stdlib only).
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("zugamind.workspace")

# A bid modulator re-weights the whole bid field in place, given the current
# cycle's context, and returns the (possibly mutated) list. This is the
# pluggable extension point that replaces bespoke per-deployment priors
# (e.g. "prefer bids I can act on", "prefer bids matching an active focus
# thread") — register one via Workspace.register_modulator(). Modulators run
# BEFORE the attention schema's own modulation, so attention-health
# corrections (streak dampening, diversity cap) always have final say.
BidModulator = Callable[[List["SalienceBid"], Dict[str, Any]], List["SalienceBid"]]


# =============================================================================
# TYPES
# =============================================================================

class ThoughtType(Enum):
    """Categories of attention that can win the workspace.

    Illustrative defaults for an operational agent — extend freely; nothing
    in the workspace engine branches on the specific values.
    """
    INFRASTRUCTURE = "infrastructure"
    CODE_QUALITY = "code_quality"
    TASK_MANAGEMENT = "task_management"
    KNOWLEDGE = "knowledge"
    METACOGNITION = "metacognition"
    SCHEDULE = "schedule"
    EXTERNAL_SIGNAL = "external_signal"


@dataclass
class SalienceBid:
    """A module's bid for workspace access this cycle.

    Higher salience = more likely to win. Salience should be computed from
    real signal (scanner output, internal state) — not random chance — so
    the workspace's decisions stay explainable after the fact.
    """
    source_module: str
    content: str
    salience: float
    thought_type: ThoughtType
    emotional_valence: float = 0.0
    context: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return bool(self.content.strip()) and 0.0 <= self.salience <= 1.0


@dataclass
class WorkspaceContent:
    """The current contents of the global workspace — the winning bid."""
    bid: SalienceBid
    timestamp: datetime = field(default_factory=datetime.now)
    broadcast_complete: bool = False
    all_bids_count: int = 0
    runner_up: Optional[SalienceBid] = None

    @property
    def source_module(self) -> str:
        return self.bid.source_module

    @property
    def content(self) -> str:
        return self.bid.content

    @property
    def salience(self) -> float:
        return self.bid.salience

    @property
    def thought_type(self) -> ThoughtType:
        return self.bid.thought_type

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_module": self.source_module,
            "content": self.content,
            "salience": self.salience,
            "thought_type": self.thought_type.value,
            "emotional_valence": self.bid.emotional_valence,
            "context": self.bid.context,
            "all_bids_count": self.all_bids_count,
            "runner_up_module": self.runner_up.source_module if self.runner_up else None,
        }


# =============================================================================
# WORKSPACE MODULE BASE
# =============================================================================

class WorkspaceModule:
    """Base class for modules that bid for workspace access.

    Each module:
    1. Receives filtered scanner/trigger data via set_triggers()
    2. Computes salience from that data via generate_bid()
    3. Reacts (or not) when it loses/wins via on_broadcast()
    """
    name: str = "base"

    def __init__(self):
        self._triggers: List[Dict[str, Any]] = []

    def set_triggers(self, triggers: List[Dict[str, Any]]):
        """Receive filtered scanner output for this module's domain."""
        self._triggers = triggers

    def generate_bid(self, context: Dict[str, Any]) -> Optional[SalienceBid]:
        """Generate a salience bid. Subclasses must implement."""
        raise NotImplementedError

    def on_broadcast(self, content: WorkspaceContent):
        """Called every cycle with the winning content. Default: no reaction."""
        pass


# =============================================================================
# ATTENTION SCHEMA
# =============================================================================

def _bid_target(module: str, ctx: Dict[str, Any]) -> Optional[str]:
    """The (module, target) identity component for a bid's context.

    Most modules attend to themselves as a whole (target=None). A module
    that rotates between distinct sub-targets (e.g. "which goal", "which
    file") can set a "target" key in its bid context so streak-dampening
    and the diversity cap key on (module, target) rather than module alone
    — rotating through five different targets should read as healthy
    diversity, not a five-cycle streak on one module.
    """
    if not isinstance(ctx, dict):
        return None
    return ctx.get("target")


def _extract_target(winner: "WorkspaceContent") -> Optional[str]:
    ctx = winner.bid.context if winner and winner.bid else {}
    return _bid_target(winner.source_module, ctx)


class AttentionSchema:
    """Self-model of the workspace's own attention.

    Tracks what has been winning, detects stuck/starved attention, and
    modulates bids to keep attention healthy. Four mechanisms, all
    operating on (module, target) identity:

    1. STREAK DAMPENING : 3 consecutive wins=0.5x, 4=0.3x, 5+=0.15x (capped)
    2. DIVERSITY CAP     : soft multiplier once an identity wins 3+ of the
                           last 6 cycles, plus a HARD ceiling applied after
                           modulation (0.25 at 3+ wins, 0.15 at 4+)
    3. BLIND SPOT BOOST  : 1.4x for modules that haven't won in the last 8
                           cycles (once enough history exists)
    4. NOVELTY BONUS     : 1.1x for a bid whose identity differs from the
                           current focus
    """

    def __init__(self):
        self.current_focus: str = ""
        self.current_focus_module: str = ""
        self.current_focus_target: Optional[str] = None
        self.focus_start_time: Optional[datetime] = None
        self.recent_foci: List[Dict[str, Any]] = []
        self.module_win_counts: Dict[str, int] = {}
        self.attention_switches: int = 0
        self._total_cycles: int = 0
        # Actuator adjustments (decaying per-module boosts/penalties).
        self._adjustments: Dict[str, float] = {}

    @staticmethod
    def _identity(focus: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        return (focus.get("module"), focus.get("target"))

    @property
    def consecutive_wins(self) -> int:
        if not self.recent_foci:
            return 0
        count = 0
        current = (self.current_focus_module, self.current_focus_target)
        for f in reversed(self.recent_foci):
            if self._identity(f) == current:
                count += 1
            else:
                break
        return count

    @property
    def blind_spots(self) -> List[str]:
        """Modules that haven't won recently (once enough history exists)."""
        if self._total_cycles < 8:
            return []
        all_modules = set(self.module_win_counts.keys())
        recent_winners = {f["module"] for f in self.recent_foci[-8:]}
        spots = all_modules - recent_winners
        spots.discard("metacognition")
        return list(spots)

    @property
    def is_stuck(self) -> bool:
        """True iff the last 3 wins share identical (module, target)."""
        if len(self.recent_foci) < 3:
            return False
        last_3_ids = {self._identity(f) for f in self.recent_foci[-3:]}
        return len(last_3_ids) == 1

    def set_adjustment(self, module_name: str, delta: float):
        """Actuator hook: boost (positive) or penalize (negative) a module."""
        self._adjustments[module_name] = delta

    def decay_adjustments(self, decay_rate: float = 0.05):
        expired = []
        for module, adj in self._adjustments.items():
            if abs(adj) < 0.01:
                expired.append(module)
            else:
                self._adjustments[module] = adj * (1.0 - decay_rate)
        for k in expired:
            del self._adjustments[k]

    def modulate(self, bids: List[SalienceBid]) -> List[SalienceBid]:
        """Modulate bid salience based on attention health. Mutates in place."""
        if not bids:
            return bids

        streak = self.consecutive_wins
        current_id = (self.current_focus_module, self.current_focus_target)

        recent_id_counts: Dict[tuple, int] = {}
        for f in self.recent_foci[-6:]:
            key = self._identity(f)
            recent_id_counts[key] = recent_id_counts.get(key, 0) + 1

        for bid in bids:
            module = bid.source_module
            ctx = bid.context if isinstance(bid.context, dict) else {}
            bid_id = (module, _bid_target(module, ctx))

            adj = self._adjustments.get(module, 0.0)
            if adj != 0.0:
                bid.salience = max(0.01, min(1.0, bid.salience + adj))

            # DIVERSITY CAP (soft): identity won 3+ of last 6.
            recent_wins = recent_id_counts.get(bid_id, 0)
            if recent_wins >= 3:
                diversity_factor = max(0.1, 1.0 - (recent_wins - 2) * 0.3)
                bid.salience *= diversity_factor

            # STREAK DAMPENING: same identity winning consecutively.
            if bid_id == current_id and streak >= 3:
                if streak >= 5:
                    bid.salience *= 0.15
                    bid.salience = min(0.3, bid.salience)
                elif streak >= 4:
                    bid.salience *= 0.3
                else:
                    bid.salience *= 0.5
            else:
                if streak >= 3:
                    boost = min(1.2 + (streak - 3) * 0.15, 2.0)
                    bid.salience = min(1.0, bid.salience * boost)
                if module in self.blind_spots:
                    bid.salience = min(1.0, bid.salience * 1.4)
                if bid_id != current_id:
                    bid.salience = min(1.0, bid.salience * 1.1)

        return bids

    def apply_hard_diversity_cap(self, bids: List[SalienceBid]) -> List[Dict[str, Any]]:
        """Post-modulation HARD ceiling for an identity that's genuinely stuck
        (3+/4+ wins of the last 6 cycles). Mutates bids; returns what was capped."""
        recent_ids = [(f.get("module"), f.get("target")) for f in self.recent_foci[-6:]]
        capped: List[Dict[str, Any]] = []
        for bid in bids:
            ctx = bid.context if isinstance(bid.context, dict) else {}
            bid_id = (bid.source_module, _bid_target(bid.source_module, ctx))
            id_wins = sum(1 for rid in recent_ids if rid == bid_id)
            ceiling = 0.15 if id_wins >= 4 else (0.25 if id_wins >= 3 else None)
            if ceiling is not None and bid.salience > ceiling:
                capped.append({"module": bid.source_module, "target": bid_id[1],
                               "wins": id_wins, "from": round(bid.salience, 4), "to": ceiling})
                bid.salience = ceiling
        return capped

    def update(self, winner: WorkspaceContent, all_bids: List[SalienceBid]):
        """Update self-model after a cycle."""
        self._total_cycles += 1

        winner_target = _extract_target(winner)
        winner_id = (winner.source_module, winner_target)
        prev_id = (self.current_focus_module, self.current_focus_target)

        if winner_id != prev_id:
            self.attention_switches += 1
            self.focus_start_time = datetime.now()

        self.current_focus = winner.content[:100]
        self.current_focus_module = winner.source_module
        self.current_focus_target = winner_target

        self.recent_foci.append({
            "module": winner.source_module,
            "target": winner_target,
            "content": winner.content[:50],
            "salience": winner.salience,
            "timestamp": datetime.now().isoformat(),
        })
        if len(self.recent_foci) > 10:
            self.recent_foci = self.recent_foci[-10:]

        self.module_win_counts[winner.source_module] = (
            self.module_win_counts.get(winner.source_module, 0) + 1
        )
        self.decay_adjustments()

    def get_context(self) -> Dict[str, Any]:
        """Attention schema context — the "reportability" surface: this is
        the self-model available for prompts, dashboards, or logs every cycle."""
        return {
            "current_focus": self.current_focus,
            "current_focus_module": self.current_focus_module,
            "current_focus_target": self.current_focus_target,
            "blind_spots": self.blind_spots,
            "is_stuck": self.is_stuck,
            "attention_switches": self.attention_switches,
            "total_cycles": self._total_cycles,
            "recent_modules": [f["module"] for f in self.recent_foci[-5:]],
            "recent_identities": [
                (f.get("module"), f.get("target")) for f in self.recent_foci[-5:]
            ],
            "adjustments": dict(self._adjustments),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "current_focus": self.current_focus,
            "current_focus_module": self.current_focus_module,
            "current_focus_target": self.current_focus_target,
            "recent_foci": self.recent_foci,
            "module_win_counts": dict(self.module_win_counts),
            "attention_switches": self.attention_switches,
            "total_cycles": self._total_cycles,
            "adjustments": dict(self._adjustments),
        }

    def restore_from_dict(self, data: Dict[str, Any]):
        self.current_focus = data.get("current_focus", "")
        self.current_focus_module = data.get("current_focus_module", "")
        self.current_focus_target = data.get("current_focus_target")
        self.recent_foci = data.get("recent_foci", [])
        self.module_win_counts = data.get("module_win_counts", {})
        self.attention_switches = data.get("attention_switches", 0)
        self._total_cycles = data.get("total_cycles", 0)
        self._adjustments = data.get("adjustments", {})
        if self.current_focus:
            self.focus_start_time = datetime.now()


# =============================================================================
# WORKSPACE ENGINE
# =============================================================================

class Workspace:
    """GWT workspace engine.

    Synchronous, stdlib-only. Cycle: gather -> modulate (plugins, then the
    attention schema) -> hard cap -> select (salience^power) -> broadcast
    -> update.

    Paper mapping (see README):
      limited capacity / one winner per cycle  -> run_cycle returns ONE winner
      broadcast to all modules                 -> on_broadcast() every cycle
      competition for access                    -> salience bids + AttentionSchema
      reportability                             -> get_stats() every cycle
      deliberate modulation                     -> register_modulator()
    """

    def __init__(self, selection_power: float = 4.0):
        self._modules: List[WorkspaceModule] = []
        self._modulators: List[BidModulator] = []
        self._workspace_content: Optional[WorkspaceContent] = None
        self.attention_schema = AttentionSchema()
        self._cycle_count: int = 0
        self.last_cycle_bids: List[SalienceBid] = []
        # Higher power = more deterministic winner selection. ^4 means a 0.9
        # bid beats a 0.6 bid ~94% of the time (vs ~80% at ^2) — tuned for an
        # operational agent that should almost always act on the highest-
        # urgency item, not explore stochastically.
        self.selection_power = selection_power

    def register_module(self, module: WorkspaceModule):
        """Register a module to participate in workspace competition."""
        self._modules.append(module)
        logger.info("[Workspace] Registered module: %s", module.name)

    def register_modulator(self, modulator: BidModulator):
        """Register a bid modulator — the steerable "deliberate modulation"
        extension point. Runs BEFORE the attention schema's own modulation,
        in registration order. A modulator receives (bids, cycle_context) and
        returns the (possibly mutated) bid list."""
        self._modulators.append(modulator)

    @property
    def current_content(self) -> Optional[WorkspaceContent]:
        return self._workspace_content

    def run_cycle(self, context: Optional[Dict[str, Any]] = None) -> Optional[WorkspaceContent]:
        """One cycle: gather -> modulate -> select -> broadcast -> update.

        Args:
            context: cycle-wide context passed to every module's generate_bid
                     and every registered modulator.

        Returns:
            WorkspaceContent with the winning bid, or None if no bids.
        """
        context = context or {}
        self._cycle_count += 1

        bids = self._gather_bids(context)
        if not bids:
            logger.debug("[Workspace] No bids this cycle")
            return None

        for modulator in self._modulators:
            try:
                bids = modulator(bids, context) or bids
            except Exception as e:
                logger.warning("[Workspace] Modulator failed: %s", e)

        bids = self.attention_schema.modulate(bids)
        self.attention_schema.apply_hard_diversity_cap(bids)

        self.last_cycle_bids = list(bids)

        winner = self._select_winner(bids)
        if not winner:
            return None

        remaining = [b for b in bids if b is not winner]
        runner_up = max(remaining, key=lambda b: b.salience) if remaining else None

        content = WorkspaceContent(bid=winner, all_bids_count=len(bids), runner_up=runner_up)
        self._workspace_content = content

        self._broadcast(content)
        self.attention_schema.update(content, bids)

        logger.info(
            "[Workspace] Winner: %s (salience=%.2f, type=%s, bids=%d)",
            winner.source_module, winner.salience, winner.thought_type.value, len(bids),
        )
        return content

    def _gather_bids(self, context: Dict[str, Any]) -> List[SalienceBid]:
        bids = []
        for module in self._modules:
            try:
                bid = module.generate_bid(context)
                if bid and bid.is_valid:
                    bids.append(bid)
            except Exception as e:
                logger.warning("[Workspace] Module %s bid failed: %s", module.name, e)
        return bids

    def _select_winner(self, bids: List[SalienceBid]) -> Optional[SalienceBid]:
        """Weighted-random selection over salience**selection_power."""
        if not bids:
            return None
        weights = [b.salience ** self.selection_power for b in bids]
        total = sum(weights)
        if total == 0:
            return random.choice(bids)
        r = random.random() * total
        cumulative = 0.0
        for bid, weight in zip(bids, weights):
            cumulative += weight
            if r <= cumulative:
                return bid
        return bids[-1]

    def _broadcast(self, content: WorkspaceContent):
        for module in self._modules:
            try:
                module.on_broadcast(content)
            except Exception as e:
                logger.warning("[Workspace] Broadcast to %s failed: %s", module.name, e)
        content.broadcast_complete = True

    def get_stats(self) -> Dict[str, Any]:
        """Everything logged/reportable about this cycle and the workspace's
        running self-model — the "every cycle is fully logged" surface."""
        return {
            "cycle_count": self._cycle_count,
            "registered_modules": [m.name for m in self._modules],
            "module_count": len(self._modules),
            "attention_schema": self.attention_schema.get_context(),
            "current_content": (
                self._workspace_content.to_dict() if self._workspace_content else None
            ),
            "last_bids": [
                {"module": b.source_module, "salience": round(b.salience, 3),
                 "type": b.thought_type.value}
                for b in self.last_cycle_bids
            ],
        }


# Backwards-compatible aliases for anyone porting code from the private
# origin project's internal naming ("Monad*" was the origin's codename).
MonadThoughtType = ThoughtType
MonadAttentionSchema = AttentionSchema
MonadWorkspace = Workspace
