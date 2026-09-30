from __future__ import annotations

import pytest

from relaygrid.models import Bus, Branch, FeederSnapshot, SwitchState
from relaygrid.operations.command_bus import Command, CommandBus
from relaygrid.operations.permit_change import (
    ChangeImpact,
    ConclusionStatus,
    PermitChangeController,
    PermitRevision,
    SafetyConditions,
    affected_commands,
    affected_orders,
    boundary_diff,
    pending_steps,
    recompute_safety,
    remaining_device_ids,
    step_blockers,
)
from relaygrid.switching.interlocks import WorkTag
from relaygrid.switching.orders import (
    OrderState,
    SwitchingOrder,
    SwitchingStep,
)


def make_snapshot():
    buses = {x: Bus(x, 12.47) for x in "ABCDE"}
    branches = {
        "ab": Branch("ab", "A", "B", 0.1, 0.1, 300, switchable=False),
        "bc": Branch("bc", "B", "C", 0.1, 0.1, 300, switchable=True),
        "cd": Branch("cd", "C", "D", 0.1, 0.1, 300, switchable=True),
        "de": Branch("de", "D", "E", 0.1, 0.1, 300, switchable=True),
    }
    state = {
        "bc": SwitchState.OPEN,   # established visible break
        "cd": SwitchState.CLOSED, # closed: cannot serve as visible break
        "de": SwitchState.OPEN,
    }
    return FeederSnapshot(buses, branches, state)


def make_order(order_id, steps, state=OrderState.APPROVED, acks=None, author="alice"):
    order = SwitchingOrder(order_id)
    order.create(steps, author)
    order.check("bob")
    order.approve("carol")
    if acks:
        order.start()
        for seq, who, ts in acks:
            order.acknowledge(seq, who, ts)
    elif state == OrderState.EXECUTING:
        order.start()
    return order


def steps(*specs):
    return [SwitchingStep(i, action, dev, "open" if action == "open" else "closed", note="")
            for i, (action, dev) in enumerate(specs, 1)]


# ---------- recomputation ----------

def test_recompute_visible_missing_and_grounded():
    snap = make_snapshot()
    boundary = frozenset({"bc", "cd", "de", "S"})
    grounds = {"S": {"de", "bc"}}
    cond = recompute_safety(boundary, snap, [], grounds)
    assert "bc" in cond.visible_breaks          # open switch -> visible break
    assert "de" in cond.visible_breaks          # ground attachment provides a break
    assert "S" in cond.grounded_sections
    assert "cd" in cond.missing_visible_breaks  # closed switch -> no visible break
    assert cond.missing_visible_breaks.isdisjoint(cond.visible_breaks)


def test_recompute_non_switchable_branch_never_a_break():
    cond = recompute_safety(frozenset({"ab"}), make_snapshot(), [])
    assert "ab" in cond.missing_visible_breaks
    assert "ab" not in cond.visible_breaks


def test_recompute_dno_scoped_to_new_boundary():
    tags = [WorkTag("t1", "bc", "do_not_operate"),
            WorkTag("t2", "cd", "do_not_operate", active=False),
            WorkTag("t3", "xx", "hold")]
    cond = recompute_safety(frozenset({"bc", "cd"}), make_snapshot(), tags)
    assert cond.do_not_operate == frozenset({"bc"})

    # tag removed from boundary is released
    cond2 = recompute_safety(frozenset({"cd"}), make_snapshot(), tags)
    assert cond2.do_not_operate == frozenset()


def test_boundary_diff_symmetric():
    added, removed = boundary_diff(frozenset({"a", "b"}), frozenset({"b", "c"}))
    assert added == frozenset({"c"})
    assert removed == frozenset({"a"})


# ---------- affected pending work ----------

def test_affected_orders_only_pending_steps_touch_change():
    snap = make_snapshot()
    executing = make_order(
        "O1",
        steps(("open", "bc"), ("close", "cd"), ("open", "de")),
        acks=[(1, "op", 10.0)],  # bc already done
    )
    approved = make_order("O2", steps(("close", "cd")))
    untouched = make_order("O3", steps(("open", "de")))
    draft = SwitchingOrder("O4")
    draft.create(steps(("close", "cd")), "alice")

    hit = {o.order_id for o in affected_orders([executing, approved, untouched, draft], frozenset({"bc", "cd"}))}
    assert hit == {"O1", "O2"}          # O1 via remaining step cd, not completed bc
    assert remaining_device_ids(executing) == frozenset({"cd", "de"})
    assert [s.sequence for s in pending_steps(executing)] == [2, 3]


def test_affected_commands_pending_only():
    bus = CommandBus()
    bus.submit(Command("K1", "bc", "open", 0.0, 100.0, "dispatcher"), 1.0)
    bus.submit(Command("K2", "de", "close", 0.0, 100.0, "dispatcher"), 1.0)
    assert affected_commands(bus, frozenset({"bc"})) == ["K1"]


# ---------- full boundary-change workflow ----------

def make_controller():
    snap = make_snapshot()
    tags = [WorkTag("t1", "bc", "do_not_operate")]
    grounds = {"S": {"de", "bc"}}
    ctrl = PermitChangeController(snapshot=snap, tags=tags, ground_attachments=grounds)
    order = make_order("O1", steps(("open", "bc"), ("close", "cd")))
    ctrl.orders["O1"] = order
    bus = CommandBus()
    bus.submit(Command("K1", "cd", "close", 0.0, 1000.0, "dispatcher"), 1.0)
    bus.submit(Command("K2", "ab", "open", 0.0, 1000.0, "dispatcher"), 1.0)
    ctrl.buses["feeder1"] = bus
    permit = PermitRevision("P1", 1, frozenset({"bc"}), "planner", 5.0, "initial")
    ctrl.admit_permit(permit, "planner", 5.0)
    return ctrl, bus


def test_change_suspends_orders_and_commands():
    ctrl, bus = make_controller()
    impact = ctrl.apply_boundary_change(
        PermitRevision("P1", 2, frozenset({"bc", "cd", "S"}), "planner", 20.0, "extend"),
        "planner", 20.0,
    )
    assert isinstance(impact, ChangeImpact)
    assert impact.added == frozenset({"cd", "S"})
    assert impact.affected_order_ids == frozenset({"O1"})
    assert impact.affected_command_ids == frozenset({"K1"})
    assert ctrl.is_held("P1", "O1")
    assert ctrl.is_held("P1", "K1")
    assert not ctrl.is_held("P1", "K2")
    assert bus.is_held("K1")
    # held remote command cannot be dispatched
    with pytest.raises(PermissionError):
        bus.next_for("cd", 21.0)
    assert bus.outstanding_for("cd")  # still pending, not consumed or expired


def test_old_conclusion_invalidated_but_history_retained():
    ctrl, _ = make_controller()
    first = ctrl.ledgers["P1"].current
    ctrl.apply_boundary_change(
        PermitRevision("P1", 2, frozenset({"bc", "cd"}), "planner", 20.0), "planner", 20.0)
    ledger = ctrl.ledgers["P1"]
    assert ledger.current.status is ConclusionStatus.PENDING
    archived = ledger.conclusions[0]
    assert archived.revision == 1 and archived.status is ConclusionStatus.SUPERSEDED
    assert len(ledger.conclusions) == 2  # history retained, not overwritten
    assert ledger.current.superseded_conclusion is archived


def test_independent_reconfirmation_required():
    ctrl, _ = make_controller()
    ctrl.apply_boundary_change(
        PermitRevision("P1", 2, frozenset({"bc", "cd"}), "planner", 20.0), "planner", 20.0)
    # issuer of the new conclusion cannot confirm it
    with pytest.raises(ValueError):
        ctrl.confirm_safety("P1", "planner", 25.0)
    confirmed = ctrl.confirm_safety("P1", "inspector-lee", 25.0)
    assert confirmed.status is ConclusionStatus.CONFIRMED
    assert confirmed.confirmed_by == "inspector-lee"


def test_confirmer_must_differ_from_previous_revision_confirmer():
    ctrl, _ = make_controller()
    ctrl.ledgers["P1"].confirm("inspector-lee", 6.0)
    ctrl.apply_boundary_change(
        PermitRevision("P1", 2, frozenset({"bc", "cd"}), "planner", 20.0), "planner", 20.0)
    # same person as the superseded conclusion is not independent
    with pytest.raises(ValueError):
        ctrl.confirm_safety("P1", "inspector-lee", 25.0)
    ctrl.confirm_safety("P1", "inspector-wong", 25.0)


def test_cannot_resume_before_reconfirmation():
    ctrl, bus = make_controller()
    ctrl.apply_boundary_change(
        PermitRevision("P1", 2, frozenset({"bc", "cd"}), "planner", 20.0), "planner", 20.0)
    with pytest.raises(PermissionError):
        ctrl.release_command("P1", "K1")
    ctrl.confirm_safety("P1", "inspector-lee", 25.0)
    ctrl.release_order("P1", "O1")
    ctrl.release_command("P1", "K1")
    assert not bus.is_held("K1")
    dispatched = bus.next_for("cd", 26.0)
    assert dispatched.command_id == "K1"


def test_step_gate_blocks_while_held_and_allows_after_release():
    ctrl, _ = make_controller()
    ctrl.apply_boundary_change(
        PermitRevision("P1", 2, frozenset({"bc", "cd", "S"}), "planner", 20.0), "planner", 20.0)
    ok, reasons = ctrl.can_execute_step("P1", "O1", 1)
    assert not ok
    assert "awaiting_independent_reconfirmation" in reasons
    assert "safety_conclusion_unconfirmed" in reasons
    # independent confirmation alone is not enough: order hold must be released
    ctrl.confirm_safety("P1", "inspector-lee", 25.0)
    ok, reasons = ctrl.can_execute_step("P1", "O1", 1)
    assert not ok
    assert set(reasons) == {"awaiting_independent_reconfirmation", "do_not_operate"}
    # even after release, the DNO physical interlock still blocks opening bc
    ctrl.release_order("P1", "O1")
    ok, reasons = ctrl.can_execute_step("P1", "O1", 1)
    assert not ok and reasons == ("do_not_operate",)


def test_close_step_blocked_by_visible_break_and_ground():
    cond = SafetyConditions(frozenset({"cd"}), frozenset({"cd"}), frozenset(), frozenset())
    assert set(step_blockers(SwitchingStep(2, "close", "cd", "closed"), cond)) == {
        "visible_break_required", "section_grounded"}
    assert step_blockers(SwitchingStep(1, "verify", "cd", "open"), cond) == ()


def test_completed_steps_never_rolled_back():
    ctrl, _ = make_controller()
    executing = make_order(
        "O2", steps(("open", "de"), ("close", "cd")),
        acks=[(1, "op", 9.0)],
    )
    ctrl.orders["O2"] = executing
    ctrl.apply_boundary_change(
        PermitRevision("P1", 2, frozenset({"bc", "cd", "de"}), "planner", 20.0),
        "planner", 20.0)
    assert executing.acknowledgements[1] == ("op", 9.0)  # history intact
    # completed step cannot be (re)executed or used to move backwards
    ok, reasons = ctrl.can_execute_step("P1", "O2", 1)
    assert not ok and reasons == ("step_already_completed",)
    ok, reasons = ctrl.can_execute_step("P1", "O2", 2)
    assert not ok
    assert "awaiting_independent_reconfirmation" in reasons


def test_revision_must_advance():
    ctrl, _ = make_controller()
    with pytest.raises(ValueError):
        ctrl.apply_boundary_change(
            PermitRevision("P1", 1, frozenset({"bc", "cd"}), "planner", 20.0),
            "planner", 20.0)


def test_unknown_permit_change_rejected():
    ctrl, _ = make_controller()
    with pytest.raises(KeyError):
        ctrl.apply_boundary_change(
            PermitRevision("P9", 2, frozenset({"bc"}), "planner", 20.0), "planner", 20.0)


def test_command_bus_hold_requires_pending_command():
    bus = CommandBus()
    with pytest.raises(KeyError):
        bus.hold("ghost")
