"""Dispatch an approved plan to the testbed and switch the machines off once the order is done."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from .config import FACTORY_INFO_TOPIC, INSTRUCTIONS_TOPIC
from .models import OptimizationPlan
from .mqtt_io import connect, publish_json
from .snapshot import extract_stocks, factory_id_from_topic

INSTRUCTION_VERSION = 1


def _goal_payload(plan: OptimizationPlan) -> dict:
    goal = plan.goal
    return {
        "target_part": goal.target_part,
        "quantity": goal.quantity,
        "deadline": goal.deadline.isoformat() if goal.deadline else None,
    }


def build_instructions(plan: OptimizationPlan) -> dict:
    """Payload the testbed applies to enable the recommended machines."""
    return {
        "type": "optimization_plan",
        "version": INSTRUCTION_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "goal": _goal_payload(plan),
        "actions": [
            {
                "machine_id": rec.machine_id,
                "factory_id": rec.factory_id,
                "part": rec.part,
                "action": rec.action,
            }
            for rec in plan.recommendations
        ],
        "estimated_seconds": plan.estimated_seconds,
        "bottleneck_part": plan.bottleneck_part,
        "stocks_used": plan.stocks_used,
    }


def build_stop_instructions(plan: OptimizationPlan, produced: int) -> dict:
    """Mirror of the enable payload that switches the same machines back off."""
    return {
        "type": "optimization_plan_stop",
        "version": INSTRUCTION_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": "order_complete",
        "goal": _goal_payload(plan),
        "produced": produced,
        "actions": [
            {
                "machine_id": rec.machine_id,
                "factory_id": rec.factory_id,
                "part": rec.part,
                "action": "disable",
            }
            for rec in plan.recommendations
        ],
    }


def publish_instructions(payload: dict) -> None:
    publish_json(INSTRUCTIONS_TOPIC, payload)


def monitor_until_complete(
    plan: OptimizationPlan,
    baseline_target_stock: int,
    units_to_produce: int,
    poll_interval: float,
    max_wait_seconds: float,
) -> bool:
    """Watch the target part's stock and publish a stop payload once the order is fulfilled.

    Completion means ``current_target_stock - baseline_target_stock >= units_to_produce``.
    Returns True if the order completed and the machines were switched off.
    """
    target = plan.goal.target_part
    threshold = baseline_target_stock + units_to_produce

    print("\n===== MONITORING PRODUCTION =====")
    print(f"  Target part       : {target}")
    print(f"  Baseline stock    : {baseline_target_stock}")
    print(f"  Units to produce  : {units_to_produce}")
    print(f"  Stop threshold    : {threshold} (baseline + units_to_produce)")
    print(f"  Poll interval     : {poll_interval:g}s  |  Max wait: {max_wait_seconds:.0f}s")

    latest: dict = {}

    def on_message(_client, _userdata, msg):
        fid = factory_id_from_topic(msg.topic)
        if fid is None:
            return
        try:
            latest[fid] = json.loads(msg.payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

    client = connect()
    client.on_message = on_message
    client.subscribe(FACTORY_INFO_TOPIC, qos=1)
    client.loop_start()

    start = time.monotonic()
    last_printed = -1
    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed > max_wait_seconds:
                print(f"\n[planner] Monitor timed out after {max_wait_seconds:.0f}s. "
                      "Machines left running; publish an 'optimization_plan_stop' "
                      f"payload to '{INSTRUCTIONS_TOPIC}' to switch them off.")
                return False

            current_total = extract_stocks(latest).get(target, 0) if latest else 0
            produced = max(0, current_total - baseline_target_stock)

            # Print progress only when it changes, to keep the log readable.
            if produced != last_printed:
                pct = min(100.0, produced / units_to_produce * 100) if units_to_produce else 100.0
                print(f"  [{elapsed:6.1f}s]  {target} stock = {current_total:>6}  "
                      f"produced = {produced:>6}/{units_to_produce}  ({pct:5.1f}%)")
                last_printed = produced

            if current_total >= threshold:
                print(f"\n[planner] Order complete: produced {produced} unit(s) of {target}.")
                stop_payload = build_stop_instructions(plan, produced)
                info = client.publish(
                    INSTRUCTIONS_TOPIC, json.dumps(stop_payload, indent=2), qos=1
                )
                info.wait_for_publish(timeout=5)
                print(f"[planner] Published stop instructions to '{INSTRUCTIONS_TOPIC}' "
                      f"(disabling {len(plan.recommendations)} machine(s)).")
                return True

            time.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\n[planner] Monitor interrupted. Machines left in their current state.")
        return False
    finally:
        client.disconnect()
        client.loop_stop()
