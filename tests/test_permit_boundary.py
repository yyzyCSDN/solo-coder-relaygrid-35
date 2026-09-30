from __future__ import annotations

from dataclasses import replace

from relaygrid.models import Branch, Bus, FeederSnapshot, SwitchState
from relaygrid.operations.clearance import Clearance
from relaygrid.operations.command_bus import Command, CommandBus
from relaygrid.operations.work_permit import (
    affected_commands,
    affected_orders,
    confirm_conclusion,
    is_pending,
    pending_steps,
    recompute_conclusion,
    visible_break_points,
)
from relaygrid.services.permit_service import PermitBoundaryService
from relaygrid.switching.interlocks import WorkTag
from relaygrid.switching.orders import OrderState, SwitchingOrder, SwitchingStep


def snap() -> FeederSnapshot:
    buses = {x: Bus(x, 12.47) for x in "ABCD"}
    branches = {
        "ab": Branch("ab", "A", "B", 0.1, 0.1, 300, False),
        "bc": Branch("bc", "B", "C", 0.1, 0.1, 300, True, False),
        "cd": Branch("cd", "C", "D", 0.1, 0.1, 300, True, False),
        "ad": Branch("ad", "A", "D", 0.1, 0.1, 300, True, False),
    }
    return FeederSnapshot(
        buses, branches, {"cd": SwitchState.OPEN, "ad": SwitchState.OPEN}
    )


def make_order(order_id: str, state: OrderState, steps, acked=None) -> SwitchingOrder:
    o = SwitchingOrder(order_id)
    o.create(steps, "zhao")
    if state == OrderState.DRAFT:
        return o
    o.check("qian")
    if state == OrderState.CHECKED:
        return o
    o.approve("sun")
    if state == OrderState.APPROVED:
        return o
    o.start()
    for seq, (op, ts) in (acked or {}).items():
        o.acknowledge(seq, op, ts)
    if state == OrderState.CANCELLED:
        o.revisions[-1] = replace(o.current, state=OrderState.CANCELLED)
    return o


def scenario():
    s = snap()
    permit = Clearance("P1", "zhang", frozenset({"cd", "ad"}))
    o1 = make_order(
        "O1",
        OrderState.DRAFT,
        [
            SwitchingStep(1, "close", "cd", "closed"),
            SwitchingStep(2, "close", "ad", "closed"),
        ],
    )
    o2 = make_order(
        "O2",
        OrderState.EXECUTING,
        [
            SwitchingStep(1, "open", "ab", "open"),
            SwitchingStep(2, "close", "bc", "closed"),
        ],
        acked={1: ("li", 5.0)},
    )
    o3 = make_order(
        "O3",
        OrderState.COMPLETE,
        [SwitchingStep(1, "open", "cd", "open")],
        acked={1: ("li", 6.0)},
    )
    o4 = make_order(
        "O4",
        OrderState.CANCELLED,
        [SwitchingStep(1, "close", "ad", "closed")],
    )
    bus = CommandBus()
    rc1 = Command("RC1", "cd", "close", 0.0, 100.0, "zhao")
    rc2 = Command("RC2", "ab", "open", 0.0, 100.0, "zhao")
    rc3 = Command("RC3", "ad", "open", 0.0, 100.0, "zhao")
    bus.submit(rc1, 0.0)
    bus.submit(rc2, 0.0)
    bus.submit(rc3, 0.0)
    bus.complete(rc3, "success", 1.0, "open")
    return s, permit, [o1, o2, o3, o4], bus


def test_visible_breaks_recomputed_from_topology():
    assert visible_break_points(snap()) == frozenset({"cd", "ad"})


def test_affected_pending_orders_and_commands():
    s, permit, orders, bus = scenario()
    conclusion = recompute_conclusion(
        "c1", "P1", 1, s, permit.boundaries, [], frozenset(), 10.0
    )
    ao = affected_orders(orders, permit.boundaries, {"cd", "ad", "bc"}, conclusion)
    ac = affected_commands(bus, permit.boundaries, {"cd", "ad", "bc"}, conclusion)
    assert ao == ["O1", "O2"]
    assert ac == ["RC1"]


def test_completed_steps_never_rolled_back():
    _, _, orders, _ = scenario()
    o2 = next(o for o in orders if o.order_id == "O2")
    assert o2.acknowledgements == {1: ("li", 5.0)}
    assert [s.sequence for s in pending_steps(o2)] == [2]
    assert is_pending(o2)


def test_boundary_change_holds_and_history():
    s, permit, orders, bus = scenario()
    svc = PermitBoundaryService()
    result = svc.change_boundary(
        permit,
        {"cd", "ad", "bc"},
        now=10.0,
        by="zhao",
        reason="work extended onto bc section",
        snapshot=s,
        tags=[],
        grounded=frozenset(),
        orders=orders,
        bus=bus,
    )
    assert result.affected_orders == ("O1", "O2")
    assert result.affected_commands == ("RC1",)
    assert svc.holds(permit) == {"O1", "O2", "RC1"}
    # old conclusion retained in history but immediately stale
    assert len(svc.conclusions(permit)) == 1
    assert not result.conclusion.confirmed
    # bc is a boundary device but still closed in the field -> not isolated
    assert result.conclusion.unisolated_boundaries == ("bc",)
    assert not svc.can_continue(permit)
    # history of the change is retained
    hist = svc.history(permit)
    assert len(hist) == 1 and hist[0].boundaries == frozenset({"cd", "ad", "bc"})


def test_independent_reconfirmation_required():
    s, permit, orders, bus = scenario()
    svc = PermitBoundaryService()
    svc.change_boundary(
        permit, {"cd", "ad", "bc"}, 10.0, "zhao", "extend", s, [], frozenset(), orders, bus
    )
    # permit holder may not self-confirm
    try:
        svc.confirm(permit, "zhang", 11.0)
    except ValueError:
        pass
    else:
        raise AssertionError("holder must not self-confirm")
    # establish isolation in the field: bc is now open (visible break)
    s2 = s.copy()
    s2.switch_state["bc"] = SwitchState.OPEN
    conclusion = svc.reassess(permit, s2, [], frozenset(), 12.0)
    assert conclusion.unisolated_boundaries == ()
    # independent person confirms
    confirmed = svc.confirm(permit, "wang", 13.0)
    assert confirmed.confirmed_by == "wang"
    assert svc.can_continue(permit)
    # old conclusion stays unconfirmed in history
    history = svc.conclusions(permit)
    assert len(history) == 2 and not history[0].confirmed and history[-1].confirmed


def test_prohibited_conditions_from_tags_and_grounds():
    s = snap()
    tags = [WorkTag("T1", "cd", "hold")]
    conclusion = recompute_conclusion(
        "c1", "P1", 1, s, {"cd", "ad"}, tags, frozenset({"bc"}), 0.0
    )
    assert "cd" in conclusion.prohibited  # active work tag
    assert "bc" in conclusion.prohibited  # section grounded
    assert "cd" in conclusion.visible_breaks and "ad" in conclusion.visible_breaks


def test_confirm_requires_independent_confirmer():
    s, permit, _, _ = scenario()
    conclusion = recompute_conclusion(
        "c1", "P1", 1, s, permit.boundaries, [], frozenset(), 0.0
    )
    try:
        confirm_conclusion(conclusion, "zhang", "zhang", 1.0)
    except ValueError:
        pass
    else:
        raise AssertionError("holder must not confirm own permit")
    ok = confirm_conclusion(conclusion, "wang", "zhang", 1.0)
    assert ok.confirmed_by == "wang"
