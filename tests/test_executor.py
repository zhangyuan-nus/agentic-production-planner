import json
from datetime import datetime, timedelta, timezone

import pytest

from production_planner.executor import build_instructions, build_stop_instructions
from production_planner.models import ProductionGoal
from production_planner.optimizer import compute_plan
from production_planner.summary import summarise_plan


@pytest.fixture
def plan(example_snapshots):
    goal = ProductionGoal(target_part="p123", quantity=500, use_existing_stock=True)
    return compute_plan(goal, example_snapshots)


def test_instructions_follow_the_testbed_contract(plan):
    payload = build_instructions(plan)
    assert payload["type"] == "optimization_plan"
    assert payload["goal"] == {"target_part": "p123", "quantity": 500, "deadline": None}
    assert payload["actions"] == [
        {"machine_id": "F3M5", "factory_id": "3", "part": "p123", "action": "enable"},
        {"machine_id": "F3M4b", "factory_id": "3", "part": "p3", "action": "enable"},
        {"machine_id": "F2M3b", "factory_id": "2", "part": "p12", "action": "enable"},
    ]
    json.dumps(payload)


def test_stop_instructions_disable_the_same_machines(plan):
    payload = build_stop_instructions(plan, produced=500)
    assert payload["type"] == "optimization_plan_stop"
    assert payload["produced"] == 500
    assert [a["machine_id"] for a in payload["actions"]] == ["F3M5", "F3M4b", "F2M3b"]
    assert {a["action"] for a in payload["actions"]} == {"disable"}


def test_deadline_is_sent_as_iso_8601(example_snapshots):
    deadline = datetime(2026, 4, 17, 18, tzinfo=timezone(timedelta(hours=8)))
    plan = compute_plan(
        ProductionGoal(target_part="p1", quantity=10, deadline=deadline), example_snapshots
    )
    assert build_instructions(plan)["goal"]["deadline"] == "2026-04-17T18:00:00+08:00"


def test_summary_degrades_gracefully_without_the_llm(plan):
    class UnreachableClient:
        @property
        def models(self):
            raise ConnectionError("network unreachable")

    summary = summarise_plan(plan, client=UnreachableClient())
    assert summary.startswith("[LLM summary unavailable")
