import pytest
import simpy

from mock_testbed.__main__ import handle_message
from mock_testbed.plant import FACTORIES, STARTUP_SECONDS, Plant
from production_planner.config import STARTUP_SECONDS as PLANNER_STARTUP_SECONDS
from production_planner.executor import build_instructions, build_stop_instructions
from production_planner.models import ProductionGoal
from production_planner.optimizer import compute_plan
from production_planner.snapshot import extract_stocks


@pytest.fixture
def plant() -> Plant:
    return Plant(simpy.Environment())


def instructions(action: str, *machine_ids: str) -> dict:
    return {
        "type": "optimization_plan" if action == "enable" else "optimization_plan_stop",
        "actions": [{"machine_id": m, "factory_id": m[1], "action": action} for m in machine_ids],
    }


def snapshots(plant: Plant) -> dict[str, dict]:
    return {factory: plant.snapshot(factory) for factory in FACTORIES}


def test_enabled_machine_produces_after_the_start_up_delay(plant):
    plant.apply(instructions("enable", "F1M1"))
    plant.env.run(until=STARTUP_SECONDS + 0.5)
    assert plant.stock["P1"] == 0
    plant.env.run(until=STARTUP_SECONDS + 1.5)
    assert plant.stock["P1"] == 10


def test_combiner_is_limited_by_its_inputs(plant):
    plant.stock.update(P1=5, P2=100)
    plant.machines["F2M3"].state = 0
    plant.env.run(until=1.5)
    assert (plant.stock["P12"], plant.stock["P1"], plant.stock["P2"]) == (5, 0, 95)


def test_instructions_are_idempotent(plant):
    plant.apply(instructions("enable", "F1M1"))
    plant.env.run(until=5.5)
    assert plant.apply(instructions("enable", "F1M1")) == ["F1M1: already enabled"]
    assert plant.machines["F1M1"].startup_left == STARTUP_SECONDS - 5

    plant.apply(instructions("disable", "F1M1"))
    assert plant.machines["F1M1"].state == -1
    assert plant.apply(instructions("disable", "F1M1")) == ["F1M1: already disabled"]


def test_unknown_or_misrouted_machines_are_skipped(plant):
    payload = {
        "type": "optimization_plan",
        "actions": [
            {"machine_id": "F9M9", "factory_id": "9", "action": "enable"},
            {"machine_id": "F1M1", "factory_id": "2", "action": "enable"},
        ],
    }
    assert all("skipped" in line for line in plant.apply(payload))
    assert plant.machines["F1M1"].state == -1


def test_enable_revives_a_forced_down_machine_like_the_testbed(plant):
    plant.set_forced_down("F3M5b", True)
    plant.apply(instructions("enable", "F3M5b"))
    assert plant.machines["F3M5b"].state == 0


def test_mqtt_messages_are_routed(plant):
    assert handle_message(plant, "F3M5 down", b"true") == ["F3M5: forced down"]
    assert plant.machines["F3M5"].state == -2
    assert "not valid JSON" in handle_message(plant, "optimizer/instructions", b"{oops")[0]


def test_snapshots_use_the_testbed_format(plant):
    plant.stock["P1"] = 7
    plant.set_forced_down("F3M5b", True)
    snaps = snapshots(plant)
    assert snaps["3"]["machines_information"]["Machine 5b"]["state"] == -2
    assert snaps["1"]["container_information"]["Container 1"] == {"part": "P1", "amount": 7}
    assert extract_stocks(snaps)["p1"] == 7


def test_seeding_from_the_example_snapshot(plant, example_snapshots):
    plant.seed(example_snapshots)
    assert plant.machines["F1M1"].state == 2
    assert plant.machines["F3M5b"].state == -2
    assert (plant.stock["P1"], plant.stock["P2"], plant.stock["P3"]) == (120, 80, 35)


def test_planner_estimate_matches_the_mock(plant):
    plan = compute_plan(ProductionGoal(target_part="p123", quantity=500), snapshots(plant))
    plant.apply(build_instructions(plan))
    finish = plan.estimated_seconds + PLANNER_STARTUP_SECONDS

    plant.env.run(until=finish - 0.5)
    assert plant.stock["P123"] < 500
    plant.env.run(until=finish + 0.5)
    assert plant.stock["P123"] >= 500

    plant.apply(build_stop_instructions(plan, produced=plant.stock["P123"]))
    assert all(m.state == -1 for m in plant.machines.values())
