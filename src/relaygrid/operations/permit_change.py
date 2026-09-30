"""Recalculation of switching safety conditions after a work-permit boundary change.

When the boundary of an active work permit is revised, every pending switching
order and remote-control command whose remaining work touches the changed
boundary must be suspended, its old safety conclusion is immediately invalid
(but retained in history), and visible breaks, grounding and do-not-operate
conditions must be recomputed and independently reconfirmed before work can
resume.  Already completed steps are never rolled back.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from relaygrid.models import FeederSnapshot, SwitchState
from relaygrid.operations.command_bus import CommandBus
from relaygrid.switching.interlocks import WorkTag
from relaygrid.switching.orders import OrderState, SwitchingOrder, SwitchingStep


class ConclusionStatus(str, Enum):
    SUPERSEDED = "superseded"
    PENDING = "pending"
    CONFIRMED = "confirmed"


_ACTIVE_ORDER_STATES = frozenset({OrderState.APPROVED, OrderState.EXECUTING})


@dataclass(frozen=True)
class PermitRevision:
    permit_id: str
    revision: int
    boundary: frozenset[str]
    author: str
    timestamp: float
    note: str = ""


@dataclass(frozen=True)
class SafetyConditions:
    visible_breaks: frozenset[str]
    grounded_sections: frozenset[str]
    do_not_operate: frozenset[str]
    missing_visible_breaks: frozenset[str]


@dataclass(frozen=True)
class SafetyConclusion:
    permit_id: str
    revision: int
    conditions: SafetyConditions
    status: ConclusionStatus
    issued_by: str
    timestamp: float
    confirmed_by: str | None = None
    confirmed_at: float | None = None
    superseded_conclusion: "SafetyConclusion | None" = None


@dataclass(frozen=True)
class ChangeImpact:
    permit_id: str
    old_revision: int
    new_revision: int
    added: frozenset[str]
    removed: frozenset[str]
    affected_order_ids: frozenset[str]
    affected_command_ids: frozenset[str]
    conditions: SafetyConditions


class SafetyLedger:
    """Append-only history of safety conclusions for one permit."""

    def __init__(self, permit_id: str):
        self.permit_id = permit_id
        self.conclusions: list[SafetyConclusion] = []

    @property
    def current(self) -> SafetyConclusion:
        if not self.conclusions:
            raise ValueError("no conclusion issued")
        return self.conclusions[-1]

    def issue(self, revision: int, conditions: SafetyConditions, issued_by: str, timestamp: float) -> SafetyConclusion:
        previous = self.conclusions[-1] if self.conclusions else None
        if previous is not None and revision <= previous.revision:
            raise ValueError("permit revisions must advance")
        if previous is not None and previous.status is not ConclusionStatus.SUPERSEDED:
            previous = self._with_status(previous, ConclusionStatus.SUPERSEDED)
            self.conclusions[-1] = previous
        conclusion = SafetyConclusion(
            self.permit_id, revision, conditions, ConclusionStatus.PENDING,
            issued_by, timestamp, superseded_conclusion=previous,
        )
        self.conclusions.append(conclusion)
        return conclusion

    def confirm(self, confirmer: str, timestamp: float) -> SafetyConclusion:
        current = self.current
        if current.status is not ConclusionStatus.PENDING:
            raise ValueError("conclusion not awaiting confirmation")
        prior_confirmers = self._prior_confirmers(current)
        if confirmer in {current.issued_by, *prior_confirmers}:
            raise ValueError("independent confirmer required")
        confirmed = SafetyConclusion(
            current.permit_id, current.revision, current.conditions,
            ConclusionStatus.CONFIRMED, current.issued_by, current.timestamp,
            confirmer, timestamp, current.superseded_conclusion,
        )
        self.conclusions[-1] = confirmed
        return confirmed

    def _prior_confirmers(self, conclusion: SafetyConclusion) -> set[str]:
        out: set[str] = set()
        prior = conclusion.superseded_conclusion
        while prior is not None:
            if prior.confirmed_by is not None:
                out.add(prior.confirmed_by)
            prior = prior.superseded_conclusion
        return out

    @staticmethod
    def _with_status(conclusion: SafetyConclusion, status: ConclusionStatus) -> SafetyConclusion:
        return SafetyConclusion(
            conclusion.permit_id, conclusion.revision, conclusion.conditions,
            status, conclusion.issued_by, conclusion.timestamp,
            conclusion.confirmed_by, conclusion.confirmed_at,
            conclusion.superseded_conclusion,
        )


def boundary_diff(old: frozenset[str], new: frozenset[str]) -> tuple[frozenset[str], frozenset[str]]:
    return frozenset(new - old), frozenset(old - new)


def recompute_safety(
    boundary: frozenset[str],
    snapshot: FeederSnapshot,
    tags: list[WorkTag],
    ground_attachments: dict[str, set[str]] | None = None,
) -> SafetyConditions:
    """Recompute the three blocking conditions for a permit boundary.

    ``boundary`` may contain bus/section identifiers and switch/branch
    identifiers.  ``ground_attachments`` maps a grounded section/bus to the
    branches that connect it to the network.
    """
    ground_attachments = ground_attachments or {}
    visible: set[str] = set()
    missing: set[str] = set()
    grounded_sections: set[str] = set()
    dno: set[str] = {t.branch_id for t in tags if t.active and t.kind == "do_not_operate"} & set(boundary)

    for element in boundary:
        branch = snapshot.branches.get(element)
        if branch is None:
            continue
        if not branch.switchable:
            missing.add(element)
        elif snapshot.switch_state.get(element, SwitchState.CLOSED) == SwitchState.OPEN:
            visible.add(element)
        else:
            missing.add(element)

    for section, attached in ground_attachments.items():
        if section in boundary and attached:
            grounded_sections.add(section)
            visible.update(b for b in attached if b in boundary)

    return SafetyConditions(
        frozenset(visible),
        frozenset(grounded_sections),
        frozenset(dno),
        frozenset(missing),
    )


def pending_steps(order: SwitchingOrder) -> list[SwitchingStep]:
    done = set(order.acknowledgements)
    return [s for s in order.current.steps if s.sequence not in done]


def remaining_device_ids(order: SwitchingOrder) -> frozenset[str]:
    return frozenset(s.device_id for s in pending_steps(order))


def affected_orders(orders: list[SwitchingOrder], changed_elements: frozenset[str]) -> list[SwitchingOrder]:
    return [
        o for o in orders
        if o.current.state in _ACTIVE_ORDER_STATES
        if remaining_device_ids(o) & changed_elements
    ]


def affected_commands(bus: CommandBus, changed_elements: frozenset[str]) -> list[str]:
    return [c.command_id for c in bus.pending if c.device_id in changed_elements]


def step_blockers(step: SwitchingStep, conditions: SafetyConditions) -> tuple[str, ...]:
    reasons: list[str] = []
    if step.action == "close":
        if step.device_id in conditions.visible_breaks:
            reasons.append("visible_break_required")
        if step.device_id in conditions.grounded_sections:
            reasons.append("section_grounded")
    if step.action in {"open", "close"} and step.device_id in conditions.do_not_operate:
        reasons.append("do_not_operate")
    return tuple(reasons)


@dataclass
class PermitChangeController:
    """Applies a permit-boundary revision and holds follow-on work.

    Holds are advisory safety gates layered on top of SwitchingOrder and
    CommandBus; the order model itself still forbids stepping backwards or
    acknowledging out of sequence.
    """

    snapshot: FeederSnapshot
    tags: list[WorkTag] = field(default_factory=list)
    ground_attachments: dict[str, set[str]] = field(default_factory=dict)
    orders: dict[str, SwitchingOrder] = field(default_factory=dict)
    buses: dict[str, CommandBus] = field(default_factory=dict)
    permits: dict[str, PermitRevision] = field(default_factory=dict)
    ledgers: dict[str, SafetyLedger] = field(default_factory=dict)
    suspended: dict[str, set[str]] = field(default_factory=dict)

    def admit_permit(self, revision: PermitRevision, issued_by: str, timestamp: float) -> SafetyConclusion:
        """Register an initial permit; no boundary change, no holds raised."""
        if revision.permit_id in self.permits:
            raise ValueError("permit already admitted")
        conditions = recompute_safety(revision.boundary, self.snapshot, self.tags, self.ground_attachments)
        ledger = SafetyLedger(revision.permit_id)
        conclusion = ledger.issue(revision.revision, conditions, issued_by, timestamp)
        self.permits[revision.permit_id] = revision
        self.ledgers[revision.permit_id] = ledger
        self.suspended[revision.permit_id] = set()
        return conclusion

    def apply_boundary_change(self, revision: PermitRevision, issued_by: str, timestamp: float) -> ChangeImpact:
        previous = self.permits.get(revision.permit_id)
        if previous is None:
            raise KeyError(revision.permit_id)
        if revision.revision <= previous.revision:
            raise ValueError("permit revisions must advance")

        added, removed = boundary_diff(previous.boundary, revision.boundary)
        changed = added | removed
        conditions = recompute_safety(revision.boundary, self.snapshot, self.tags, self.ground_attachments)
        self.ledgers[revision.permit_id].issue(revision.revision, conditions, issued_by, timestamp)

        hit_orders = affected_orders(list(self.orders.values()), changed)
        hit_commands: list[str] = []
        for bus in self.buses.values():
            for command_id in affected_commands(bus, changed):
                bus.hold(command_id)
                hit_commands.append(command_id)

        held = self.suspended[revision.permit_id]
        held.update(o.order_id for o in hit_orders)
        held.update(hit_commands)

        self.permits[revision.permit_id] = revision
        return ChangeImpact(
            revision.permit_id, previous.revision, revision.revision,
            added, removed,
            frozenset(o.order_id for o in hit_orders),
            frozenset(hit_commands),
            conditions,
        )

    def confirm_safety(self, permit_id: str, confirmer: str, timestamp: float) -> SafetyConclusion:
        return self.ledgers[permit_id].confirm(confirmer, timestamp)

    def release_order(self, permit_id: str, order_id: str) -> None:
        self.suspended[permit_id].discard(order_id)

    def release_command(self, permit_id: str, command_id: str) -> None:
        if self.ledgers[permit_id].current.status is not ConclusionStatus.CONFIRMED:
            raise PermissionError("safety not reconfirmed")
        self.suspended[permit_id].discard(command_id)
        for bus in self.buses.values():
            if bus.is_held(command_id):
                bus.release_hold(command_id)
                return
        raise KeyError(command_id)

    def is_held(self, permit_id: str, entity_id: str) -> bool:
        return entity_id in self.suspended[permit_id]

    def can_execute_step(self, permit_id: str, order_id: str, sequence: int) -> tuple[bool, tuple[str, ...]]:
        order = self.orders[order_id]
        if order.current.state not in _ACTIVE_ORDER_STATES:
            return False, ("order_not_active",)
        step = next((s for s in order.current.steps if s.sequence == sequence), None)
        if step is None:
            raise KeyError(sequence)
        if sequence in order.acknowledgements:
            return False, ("step_already_completed",)
        expected = min(s.sequence for s in pending_steps(order))
        if sequence != expected:
            return False, ("step_out_of_order",)
        reasons: list[str] = []
        if self.is_held(permit_id, order_id):
            reasons.append("awaiting_independent_reconfirmation")
        conclusion = self.ledgers[permit_id].current
        if conclusion.status is not ConclusionStatus.CONFIRMED:
            reasons.append("safety_conclusion_unconfirmed")
        reasons.extend(step_blockers(step, conclusion.conditions))
        return (not reasons), tuple(reasons)
