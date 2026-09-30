"""工作许可边界变更控制。

当工作许可的边界发生变化时，所有剩余工作触及变更区域的待执行操作票和
待执行遥控命令均受影响。安全结论依据实时拓扑（可见断点）、已装设接地线
与联锁（禁止操作条件）重新计算。

旧安全结论绝不删除：它们保留在许可历史中，但立即失效。在独立人员（非
许可持有人）重新确认新结论之前，工作不得继续。已完成的操作步骤与已完成
的遥控命令一律不得倒退。
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from relaygrid.models import FeederSnapshot, SwitchState
from relaygrid.switching.interlocks import can_close
from relaygrid.switching.orders import OrderState, SwitchingOrder
from relaygrid.operations.command_bus import CommandBus


@dataclass(frozen=True)
class SafetyConclusion:
    """重新计算得出的许可安全依据。"""

    conclusion_id: str
    permit_id: str
    revision: int
    visible_breaks: frozenset[str]        # 可见断点
    grounded_sections: frozenset[str]     # 接地段
    prohibited: dict[str, tuple[str, ...]]  # 禁止操作条件：branch_id -> 原因
    unisolated_boundaries: tuple[str, ...]  # 既不可见断开也未接地的边界设备
    computed_at: float
    confirmed_by: str | None = None
    confirmed_at: float | None = None

    @property
    def confirmed(self) -> bool:
        return self.confirmed_by is not None


@dataclass(frozen=True)
class BoundaryRevision:
    """许可边界的一次变更记录（历史保留，不可删除）。"""

    revision: int
    boundaries: frozenset[str]
    changed_at: float
    changed_by: str
    reason: str


def visible_break_points(snapshot: FeederSnapshot) -> frozenset[str]:
    """当前拓扑中保持断开的可开关支路——即可见隔离断点。"""
    return frozenset(
        br.branch_id
        for br in snapshot.branches.values()
        if br.switchable
        and snapshot.switch_state.get(br.branch_id, SwitchState.CLOSED) == SwitchState.OPEN
    )


def recompute_conclusion(
    conclusion_id: str,
    permit_id: str,
    revision: int,
    snapshot: FeederSnapshot,
    boundaries: frozenset[str],
    tags,
    grounded: frozenset[str] | set[str] | tuple[str, ...],
    computed_at: float,
) -> SafetyConclusion:
    breaks = visible_break_points(snapshot)
    grounded_f = frozenset(grounded)
    prohibited: dict[str, tuple[str, ...]] = {}
    for br in snapshot.branches.values():
        if not br.switchable:
            continue
        ok, reasons = can_close(br.branch_id, tags, breaks, grounded_f)
        if not ok:
            prohibited[br.branch_id] = tuple(reasons)
    unisolated = tuple(
        sorted(
            b
            for b in frozenset(boundaries)
            if b not in breaks and b not in grounded_f
        )
    )
    return SafetyConclusion(
        conclusion_id,
        permit_id,
        revision,
        breaks,
        grounded_f,
        prohibited,
        unisolated,
        computed_at,
    )


def confirm_conclusion(
    conclusion: SafetyConclusion,
    confirmer: str,
    permit_holder: str,
    confirmed_at: float,
) -> SafetyConclusion:
    """由独立人员重新确认安全结论；许可持有人不得自行确认。"""
    if conclusion.confirmed:
        raise ValueError("conclusion already confirmed")
    if confirmer == permit_holder:
        raise ValueError("independent confirmation required")
    return replace(conclusion, confirmed_by=confirmer, confirmed_at=confirmed_at)


def pending_steps(order: SwitchingOrder):
    """尚未确认的步骤——已完成步骤不得倒退。"""
    rev = order.current
    done = set(order.acknowledgements)
    return [s for s in rev.steps if s.sequence not in done]


def is_pending(order: SwitchingOrder) -> bool:
    return order.current.state not in {OrderState.COMPLETE, OrderState.CANCELLED}


def affected_orders(
    orders,
    old_boundaries,
    new_boundaries,
    conclusion: SafetyConclusion,
) -> list[str]:
    """剩余工作触及变更区域或新禁止条件的待执行操作票。"""
    changed = frozenset(old_boundaries) ^ frozenset(new_boundaries)
    affected: list[str] = []
    for order in orders:
        if not is_pending(order):
            continue
        for step in pending_steps(order):
            if step.device_id in changed or step.device_id in conclusion.prohibited:
                affected.append(order.order_id)
                break
    return affected


def affected_commands(
    bus: CommandBus,
    old_boundaries,
    new_boundaries,
    conclusion: SafetyConclusion,
) -> list[str]:
    """设备触及变更区域或新禁止条件的待执行遥控命令。"""
    changed = frozenset(old_boundaries) ^ frozenset(new_boundaries)
    return [
        c.command_id
        for c in bus.pending
        if c.command_id not in bus.completed
        and (c.device_id in changed or c.device_id in conclusion.prohibited)
    ]
