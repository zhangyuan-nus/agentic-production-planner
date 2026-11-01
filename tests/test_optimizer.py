from datetime import datetime, timedelta, timezone

import pytest

from production_planner.models import ProductionGoal
from production_planner.optimizer import (
    _compute_net_demand,
    _transitive_dependencies,
    compute_plan,
)

NOW = datetime(2026, 4, 17, 8, 0, tzinfo=timezone.utc)


def enabled(plan) -> list[str]:
    return [rec.machine_id for rec in plan.recommendations]


def test_dependencies_are_listed_leaves_first():
    assert _transitive_dependencies("p123") == ["p1", "p2", "p12", "p3", "p123"]
    assert _transitive_dependencies("p234") == ["p2", "p3", "p23", "p4", "p234"]
    assert _transitive_dependencies("p4") == ["p4"]


def test_without_stock_every_stage_makes_the_full_quantity():
    net, used = _compute_net_demand("p123", 50, {})
    assert net == {"p123": 50, "p12": 50, "p1": 50, "p2": 50, "p3": 50}
    assert used == {}


def test_intermediate_stock_reduces_upstream_demand():
    net, used = _compute_net_demand("p123", 50, {"p12": 30, "p1": 100})
    assert net == {"p123": 50, "p12": 20, "p1": 0, "p2": 20, "p3": 50}
    assert used == {"p12": 30, "p1": 20}


def test_shared_component_stock_is_only_counted_once():
    # Hypothetical diamond BOM: both branches of "top" consume part "x".
    bom = {"top": ["a", "b"], "a": ["x"], "b": ["x"], "x": []}
    net, used = _compute_net_demand("top", 8, {"x": 10}, dependencies=bom)
    assert used == {"x": 10}
    assert net["x"] == 6


def test_order_covered_by_stock_needs_no_machines(make_snapshots):
    goal = ProductionGoal(target_part="p1", quantity=100, use_existing_stock=True)
    plan = compute_plan(goal, make_snapshots(stock={"p1": 120}))
    assert plan.feasible
    assert plan.recommendations == []
    assert plan.estimated_seconds == 0
    assert plan.stocks_used == {"p1": 100}


def test_stock_is_ignored_unless_requested(make_snapshots):
    goal = ProductionGoal(target_part="p1", quantity=100)
    plan = compute_plan(goal, make_snapshots(stock={"p1": 120}))
    assert plan.stocks_used == {}
    assert enabled(plan) == ["F1M1", "F1M1b"]


def test_trim_pass_drops_machines_that_do_not_shorten_the_plan(make_snapshots):
    # Stock leaves p1 with 20 units to make, so one p1 machine (2 s) already
    # beats the 5 s the doubled p2 and p12 stages need for 100 units.
    goal = ProductionGoal(target_part="p12", quantity=100, use_existing_stock=True)
    plan = compute_plan(goal, make_snapshots(stock={"p1": 80}))
    assert sorted(enabled(plan)) == ["F1M1", "F2M2", "F2M2b", "F2M3", "F2M3b"]
    assert plan.estimated_seconds == 5.0
    assert any("Trim pass removed 1" in w for w in plan.warnings)


def test_running_machines_contribute_their_current_rate(make_snapshots):
    # F1M1 is already running in state 3 (5 units/s), so only its backup is enabled.
    plan = compute_plan(ProductionGoal(target_part="p1", quantity=50), make_snapshots({"F1M1": 3}))
    assert enabled(plan) == ["F1M1b"]
    assert plan.stages[0].active_machines == ["F1M1", "F1M1b"]
    assert plan.stages[0].capacity_per_second == 15.0


def test_machine_under_maintenance_adds_no_capacity(make_snapshots):
    plan = compute_plan(ProductionGoal(target_part="p1", quantity=50), make_snapshots({"F1M1": 7}))
    assert enabled(plan) == ["F1M1b"]
    assert plan.stages[0].capacity_per_second == 10.0


def test_forced_down_machines_are_never_enabled(make_snapshots):
    plan = compute_plan(
        ProductionGoal(target_part="p123", quantity=100), make_snapshots({"F3M5b": -2})
    )
    assert "F3M5" in enabled(plan)
    assert "F3M5b" not in enabled(plan)
    assert any("F3M5b" in w and "forced down" in w for w in plan.warnings)


def test_stage_without_a_usable_machine_is_infeasible(make_snapshots):
    plan = compute_plan(
        ProductionGoal(target_part="p1", quantity=10),
        make_snapshots({"F1M1": -2, "F1M1b": -2}),
    )
    assert not plan.feasible
    assert plan.recommendations == []


def test_unknown_part_is_infeasible(make_snapshots):
    plan = compute_plan(ProductionGoal(target_part="p9", quantity=10), make_snapshots())
    assert not plan.feasible
    assert "Unknown part" in plan.warnings[0]


@pytest.mark.parametrize(("seconds_until_deadline", "met"), [(20, True), (12, False)])
def test_deadline_check_includes_machine_startup(make_snapshots, seconds_until_deadline, met):
    # Two p1 machines make 100 units in 5 s, plus 10 s to start up: 15 s in total.
    deadline = NOW + timedelta(seconds=seconds_until_deadline)
    goal = ProductionGoal(target_part="p1", quantity=100, deadline=deadline)
    plan = compute_plan(goal, make_snapshots(), now=NOW)
    assert plan.estimated_seconds == 5.0
    assert plan.deadline_met is met


def test_naive_deadline_is_read_as_local_time(make_snapshots):
    goal = ProductionGoal(target_part="p1", quantity=1, deadline=datetime(2026, 4, 17, 18, 0))
    assert goal.deadline.tzinfo is not None
    assert compute_plan(goal, make_snapshots(), now=NOW).deadline_met is not None


def test_example_scenario(example_snapshots):
    goal = ProductionGoal(target_part="p123", quantity=500, use_existing_stock=True)
    plan = compute_plan(goal, example_snapshots)
    assert enabled(plan) == ["F3M5", "F3M4b", "F2M3b"]
    assert plan.estimated_seconds == 50.0
    assert plan.bottleneck_part == "p123"
    assert plan.stocks_used == {"p1": 120, "p2": 80, "p3": 35}
