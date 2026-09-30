"""工作许可边界变更的编排服务。

每次边界变更都会：保留一条不可删除的变更历史、立即作废旧安全结论并
生成一条新的待确认结论、锁定所有受影响的待执行操作票与遥控命令。
只有在独立人员重新确认最新结论、且全部边界设备均已隔离（可见断开或
接地）之后，工作才能继续。已完成步骤与已完成命令绝不回退。
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from relaygrid.operations.clearance import Clearance
from relaygrid.operations.work_permit import (
    BoundaryRevision,
    SafetyConclusion,
    affected_commands,
    affected_orders,
    confirm_conclusion,
    recompute_conclusion,
)


@dataclass(frozen=True)
class BoundaryChangeResult:
    permit: Clearance
    revision: BoundaryRevision
    affected_orders: tuple[str, ...]
    affected_commands: tuple[str, ...]
    conclusion: SafetyConclusion


class PermitBoundaryService:
    """按许可保存边界变更历史与安全结论，并维护受影响工作的锁定。"""

    def __init__(self):
        self._revisions: dict[str, list[BoundaryRevision]] = {}
        self._conclusions: dict[str, list[SafetyConclusion]] = {}
        self._boundaries: dict[str, frozenset[str]] = {}
        self._holds: dict[str, set[str]] = {}

    def change_boundary(
        self,
        permit: Clearance,
        new_boundaries,
        now: float,
        by: str,
        reason: str,
        snapshot,
        tags,
        grounded,
        orders,
        bus,
    ) -> BoundaryChangeResult:
        old = permit.boundaries
        permit_id = permit.clearance_id
        rev_no = len(self._revisions.get(permit_id, [])) + 1
        new_b = frozenset(new_boundaries)
        revision = BoundaryRevision(rev_no, new_b, now, by, reason)
        self._revisions.setdefault(permit_id, []).append(revision)
        self._boundaries[permit_id] = new_b

        conclusion = self._recompute(
            permit_id, rev_no, new_b, snapshot, tags, grounded, now
        )
        affected_o = affected_orders(orders, old, new_b, conclusion)
        affected_c = affected_commands(bus, old, new_b, conclusion)
        self._holds.setdefault(permit_id, set()).update(affected_o)
        self._holds[permit_id].update(affected_c)

        new_permit = replace(permit, boundaries=new_b)
        return BoundaryChangeResult(
            new_permit,
            revision,
            tuple(affected_o),
            tuple(affected_c),
            conclusion,
        )

    def reassess(
        self,
        permit: Clearance,
        snapshot,
        tags,
        grounded,
        now: float,
    ) -> SafetyConclusion:
        """边界不变，依据当前现场条件（接地/断开已变更）重新计算结论。"""
        permit_id = permit.clearance_id
        rev_no = len(self._revisions.get(permit_id, []))
        if rev_no == 0:
            raise ValueError("no boundary revision on record")
        return self._recompute(
            permit_id,
            rev_no,
            self._boundaries[permit_id],
            snapshot,
            tags,
            grounded,
            now,
        )

    def _recompute(
        self,
        permit_id: str,
        rev_no: int,
        boundaries,
        snapshot,
        tags,
        grounded,
        now: float,
    ) -> SafetyConclusion:
        seq = len(self._conclusions.get(permit_id, [])) + 1
        conclusion = recompute_conclusion(
            f"{permit_id}-c{seq}",
            permit_id,
            rev_no,
            snapshot,
            frozenset(boundaries),
            tags,
            grounded,
            now,
        )
        self._conclusions.setdefault(permit_id, []).append(conclusion)
        return conclusion

    def confirm(
        self, permit: Clearance, confirmer: str, now: float
    ) -> SafetyConclusion:
        """独立人员重新确认最新安全结论；边界未隔离时不得确认。"""
        conclusions = self._conclusions.get(permit.clearance_id, [])
        if not conclusions:
            raise ValueError("no safety conclusion on record")
        latest = conclusions[-1]
        if latest.confirmed:
            raise ValueError("conclusion already confirmed")
        if latest.unisolated_boundaries:
            raise ValueError("boundary devices not isolated")
        confirmed = confirm_conclusion(latest, confirmer, permit.holder, now)
        conclusions[-1] = confirmed
        return confirmed

    def release_hold(self, permit: Clearance, item_id: str) -> None:
        self._holds.get(permit.clearance_id, set()).discard(item_id)

    def can_continue(self, permit: Clearance) -> bool:
        if permit.released:
            return False
        conclusions = self._conclusions.get(permit.clearance_id, [])
        if not conclusions:
            return False
        latest = conclusions[-1]
        return latest.confirmed and not latest.unisolated_boundaries

    def holds(self, permit: Clearance) -> set[str]:
        return set(self._holds.get(permit.clearance_id, set()))

    def history(self, permit: Clearance) -> list[BoundaryRevision]:
        return list(self._revisions.get(permit.clearance_id, []))

    def conclusions(self, permit: Clearance) -> list[SafetyConclusion]:
        return list(self._conclusions.get(permit.clearance_id, []))
