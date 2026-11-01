"""
Standalone optimisation runner.

Usage (structured flags):
    python -m production_planner.optimization_runner --part p123 --qty 500
    python -m production_planner.optimization_runner --part p12 --qty 200 --deadline "2026-04-01T18:00:00"
    python -m production_planner.optimization_runner --part p234 --qty 500 --use-existing-stock

Usage (natural-language order, goes through the Order Agent LLM):
    python -m production_planner.optimization_runner --order "make 500 p12 by tomorrow 8am, use existing stock"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from google import genai

from .config import MODEL, MQTT_BROKER, PART_TO_MACHINE
from .models import OptimizationPlan, ProductionGoal
from .optimizer import compute_plan
from .order_agent import intent_to_goal, parse_order, print_intent
from .snapshot import extract_stocks, get_all_factory_snapshots


INSTRUCTIONS_TOPIC = "optimizer/instructions"
STOP_INSTRUCTIONS_TOPIC = "optimizer/instructions"  # same topic, different payload.type

LLM_SUMMARY_PROMPT = """\
You are a factory production planning assistant.

Below is the JSON output of an algorithmic optimiser that computed a production
plan for a factory network.  Your job is to turn it into a clear, actionable
report for a plant manager.

Notes on units and model:
- All times are in SIMULATION SECONDS (1 sim timestep = 1 second).
- Each active machine's rate depends on its current health state:
  state 0 -> 10/s, 1 -> 9, 2 -> 8, 3 -> 5, 4-9 -> 0.
- A machine recommended for enable is assumed to start at state 0 (10/s).
- `estimated_seconds` does NOT include startup ramp-up, lorry travel time
  between factories, or possible throughput de-rating from internal transport
  caps (c_m_move, m_c_move at 1 unit/step); those are surfaced in `warnings`.
- Each stage has `existing_stock` and `required_units`. If `goal.use_existing_stock`
  is true, the optimiser already subtracted `existing_stock` from the order
  quantity at that stage. Mention stock usage in your report when it materially
  reduces the plan.

Structure your response as:
1. **Goal recap** – one sentence restating part, quantity, and deadline.
2. **Existing stock usage** – ALWAYS include this section:
   - If `goal.use_existing_stock` is false: state "Existing stock not used;
     planning from scratch."
   - If true and `stocks_used` is non-empty: list each part and how many units
     are being drawn from existing stock, e.g. "p1: 80 units drawn from
     existing stock".
   - If true but `stocks_used` is empty: state "Stock check requested but no
     existing stock found."
3. **Feasibility verdict** – clearly state whether the goal can be met.
4. **Key numbers** – estimated seconds, max throughput per second, bottleneck stage.
5. **Recommended actions** – numbered list of machines to enable (machine ID,
   factory, part produced, capacity added).
6. **Risks & warnings** – any warnings from the optimiser, plus your own
   observations (e.g. single-point-of-failure stages, tight deadlines,
   startup ramp-up, cross-factory transport).
7. **Next steps** – 2-3 concrete things the operator should do now.

Keep the report concise (≤ 600 words). Use plain language, no raw JSON.
"""


def _summarise_with_llm(plan_json: str) -> str:
    try:
        client = genai.Client()
        response = client.models.generate_content(
            model=MODEL,
            contents=[LLM_SUMMARY_PROMPT, plan_json],
        )
        return (response.text or "").strip()
    except Exception as exc:
        return f"[LLM summary unavailable: {exc}]"


def _build_instructions(plan: OptimizationPlan) -> dict:
    """Build a structured instruction payload for the simulator."""
    return {
        "type": "optimization_plan",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "goal": {
            "target_part": plan.goal.target_part,
            "quantity": plan.goal.quantity,
            "deadline": plan.goal.deadline.isoformat() if plan.goal.deadline else None,
        },
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


def _publish_instructions(instructions: dict) -> None:
    """Publish the confirmed plan to MQTT."""
    payload = json.dumps(instructions, indent=2)
    c = mqtt.Client()
    c.connect(MQTT_BROKER)
    c.loop_start()
    try:
        c.publish(INSTRUCTIONS_TOPIC, payload, qos=1, retain=False)
        print(f"[optimizer] Published instructions to '{INSTRUCTIONS_TOPIC}'")
    finally:
        c.loop_stop()
        c.disconnect()


def _build_stop_instructions(plan: OptimizationPlan, produced: int) -> dict:
    """Build a 'turn off' payload that mirrors the enable schema but flips action."""
    return {
        "type": "optimization_plan_stop",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": "order_complete",
        "goal": {
            "target_part": plan.goal.target_part,
            "quantity": plan.goal.quantity,
        },
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


def _monitor_until_complete(
    plan: OptimizationPlan,
    baseline_target_stock: int,
    units_to_produce: int,
    poll_interval: float,
    max_wait_seconds: float,
) -> None:
    """Subscribe to Factory/+/Information and wait until enough target part is produced.

    When ``current_target_stock - baseline_target_stock >= units_to_produce``,
    publish a stop payload that disables all machines we enabled.
    """
    target = plan.goal.target_part
    threshold = baseline_target_stock + units_to_produce

    print("\n===== MONITORING PRODUCTION =====")
    print(f"  Target part       : {target}")
    print(f"  Baseline stock    : {baseline_target_stock}")
    print(f"  Units to produce  : {units_to_produce}")
    print(f"  Stop threshold    : {threshold} (baseline + units_to_produce)")
    print(f"  Poll interval     : {poll_interval}s  |  Max wait: {max_wait_seconds:.0f}s")

    latest: dict = {}

    def on_message(_client, _userdata, msg):
        parts = str(msg.topic).split("/")
        if len(parts) == 3 and parts[0] == "Factory" and parts[2] == "Information":
            try:
                latest[parts[1]] = json.loads(msg.payload.decode())
            except Exception:
                pass

    c = mqtt.Client()
    c.on_message = on_message
    c.connect(MQTT_BROKER)
    c.subscribe("Factory/+/Information", qos=1)
    c.loop_start()

    start = time.time()
    last_printed = -1
    try:
        while True:
            elapsed = time.time() - start
            if elapsed > max_wait_seconds:
                print(f"\n[optimizer] Monitor timeout after {max_wait_seconds:.0f}s. "
                      "Machines left running. Re-run with --no-confirm to issue stop "
                      "manually.")
                return

            current_total = extract_stocks(latest).get(target, 0) if latest else 0
            produced = max(0, current_total - baseline_target_stock)

            # Print progress only when it changes, to keep the log readable.
            if produced != last_printed:
                pct = (produced / units_to_produce * 100) if units_to_produce else 100
                print(f"  [{elapsed:6.1f}s]  {target} stock = {current_total:>6}  "
                      f"produced = {produced:>6}/{units_to_produce}  ({pct:5.1f}%)")
                last_printed = produced

            if current_total >= threshold:
                print(f"\n[optimizer] Order complete — produced {produced} unit(s) of {target}.")
                stop_payload = _build_stop_instructions(plan, produced)
                c.publish(
                    STOP_INSTRUCTIONS_TOPIC,
                    json.dumps(stop_payload, indent=2),
                    qos=1,
                    retain=False,
                )
                print(f"[optimizer] Published stop instructions to "
                      f"'{STOP_INSTRUCTIONS_TOPIC}' "
                      f"(disabling {len(plan.recommendations)} machine(s)).")
                # Give the message a moment to flush before disconnecting.
                time.sleep(0.5)
                return

            time.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\n[optimizer] Monitor interrupted by user. Machines left in their "
              "current state.")
    finally:
        c.loop_stop()
        c.disconnect()


def _print_stock_section(plan: OptimizationPlan) -> None:
    """Always-shown section stating whether existing stock was applied."""
    print("\n===== EXISTING STOCK =====")
    if not plan.goal.use_existing_stock:
        print("  Use existing stock: NO (flag --use-existing-stock not set)")
        print("  All required units will be produced from scratch.")
        return

    print("  Use existing stock: YES")
    if plan.stocks_used:
        total = sum(plan.stocks_used.values())
        print(f"  Existing parts applied to order ({total} total unit(s)):")
        for part, qty in sorted(plan.stocks_used.items()):
            print(f"    - {part}: {qty} unit(s) drawn from existing stock")
    else:
        print("  No existing stock found in snapshots (planning as if stock = 0).")

    rows = [(s.part, s.existing_stock, s.required_units)
            for s in plan.stages if s.existing_stock > 0 or s.required_units > 0]
    if rows:
        print("\n  Per-stage breakdown:")
        print(f"    {'Part':<6}  {'In stock':>10}  {'Still to produce':>18}")
        for part, have, need in rows:
            print(f"    {part:<6}  {have:>10}  {need:>18}")


def _print_action_summary(plan: OptimizationPlan) -> None:
    if not plan.recommendations:
        print("\n  No machine changes needed — current capacity is sufficient.")
        return
    print(f"\n  {len(plan.recommendations)} machine action(s):")
    for rec in plan.recommendations:
        print(f"    - {rec.action.upper()} {rec.machine_id} (Factory {rec.factory_id}, part {rec.part})")


def _confirm() -> bool:
    """Ask user for Y/N confirmation."""
    while True:
        answer = input("\nApply these actions? [Y/n]: ").strip().lower()
        if answer in ("y", "yes", ""):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please enter Y or N.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Factory production optimiser")
    parser.add_argument(
        "--order",
        default=None,
        help=("Natural-language order. If given, the Order Agent (LLM) parses it "
              "into a ProductionGoal and overrides --part/--qty/--deadline/"
              "--use-existing-stock."),
    )
    parser.add_argument("--part", default=None, help=f"Target part ({', '.join(sorted(PART_TO_MACHINE))})")
    parser.add_argument("--qty", default=None, type=int, help="Number of units to produce")
    parser.add_argument("--deadline", default=None, help="ISO-8601 deadline (e.g. 2026-04-02T08:00:00)")
    parser.add_argument(
        "--confirm-order",
        action="store_true",
        help="When using --order, ask Y/N before running the optimiser on the parsed goal.",
    )
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM summary, print raw plan only")
    parser.add_argument("--auto-confirm", action="store_true", help="Skip confirmation prompt")
    parser.add_argument("--mqtt-timeout", type=int, default=10, help="MQTT snapshot timeout in seconds")
    parser.add_argument(
        "--use-existing-stock",
        action="store_true",
        help="Subtract current container stock (read from MQTT snapshots) from per-stage demand",
    )
    parser.add_argument(
        "--no-monitor",
        action="store_true",
        help="Do NOT watch MQTT for completion / auto-stop machines after instructions are sent.",
    )
    parser.add_argument(
        "--monitor-interval",
        type=float,
        default=2.0,
        help="Seconds between completion checks (default: 2).",
    )
    parser.add_argument(
        "--monitor-timeout",
        type=float,
        default=0.0,
        help=("Give up waiting for completion after N seconds (0 = auto: "
              "2x estimated_seconds, min 300s, max 24h)."),
    )
    args = parser.parse_args()

    if args.order:
        if args.part or args.qty is not None:
            print("[optimizer] NOTE: --order overrides --part / --qty / --deadline / --use-existing-stock.")
        print(f"[order-agent] Parsing: {args.order!r}")
        try:
            intent = parse_order(args.order)
        except Exception as exc:
            print(f"[order-agent] FAILED to parse order: {exc}", file=sys.stderr)
            sys.exit(2)

        print()
        print_intent(intent)

        if intent.confidence < 0.6:
            print(f"\n[order-agent] WARNING: low confidence ({intent.confidence:.2f}). "
                  "Consider re-running with --confirm-order or rephrasing.")

        if args.confirm_order:
            ans = input("\nProceed with this parsed order? [Y/n]: ").strip().lower()
            if ans not in ("", "y", "yes"):
                print("[order-agent] Cancelled.")
                sys.exit(0)

        try:
            goal = intent_to_goal(intent)
        except Exception as exc:
            print(f"[order-agent] FAILED to build ProductionGoal: {exc}", file=sys.stderr)
            sys.exit(2)
    else:
        if not args.part or args.qty is None:
            parser.error("either --order, or both --part and --qty must be given")

        deadline = None
        if args.deadline:
            deadline = datetime.fromisoformat(args.deadline)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)

        goal = ProductionGoal(
            target_part=args.part,
            quantity=args.qty,
            deadline=deadline,
            use_existing_stock=args.use_existing_stock,
        )

    print(f"[optimizer] Fetching live factory snapshots (timeout={args.mqtt_timeout}s) ...")
    snapshots = get_all_factory_snapshots(timeout=args.mqtt_timeout)
    factories_found = list(snapshots.keys())
    print(f"[optimizer] Received snapshots from factory(ies): {factories_found or 'none'}")

    if not snapshots:
        print("[optimizer] WARNING: No snapshots received. Plan will assume all machines inactive.")

    plan = compute_plan(goal, snapshots)
    plan_json = plan.model_dump_json(indent=2)

    print("\n===== OPTIMISATION PLAN (raw) =====")
    print(plan_json)

    _print_stock_section(plan)

    if not args.no_llm:
        print("\n===== LLM SUMMARY =====")
        summary = _summarise_with_llm(plan_json)
        print(summary)

    if not plan.feasible:
        print("\n[optimizer] Plan is not feasible. No actions to apply.")
        sys.exit(1)

    all_stages_covered = all(s.required_units <= 0 for s in plan.stages)
    if all_stages_covered:
        print("\n[optimizer] Existing stock already covers the order. Nothing to produce.")
        sys.exit(0)

    if not plan.recommendations:
        print("\n[optimizer] No machine changes needed. Current capacity is sufficient.")
        sys.exit(0)

    print("\n===== PROPOSED ACTIONS =====")
    _print_action_summary(plan)

    confirmed = args.auto_confirm or _confirm()

    if not confirmed:
        print("[optimizer] Cancelled. No instructions sent.")
        return

    instructions = _build_instructions(plan)
    _publish_instructions(instructions)
    print("[optimizer] Done. Instructions sent to factory.")

    if args.no_monitor:
        print("[optimizer] --no-monitor set; not watching for completion. "
              "You must send stop instructions manually.")
        return

    baseline_total = extract_stocks(snapshots).get(goal.target_part, 0)
    units_to_produce = max(0, goal.quantity - plan.stocks_used.get(goal.target_part, 0))
    if units_to_produce <= 0:
        print("[optimizer] Nothing left to produce after stock credit. Skipping monitor.")
        return

    est = plan.estimated_seconds or 0.0
    if args.monitor_timeout > 0:
        timeout = args.monitor_timeout
    else:
        timeout = max(300.0, min(24 * 3600.0, est * 2.0 if est > 0 else 3600.0))

    _monitor_until_complete(
        plan=plan,
        baseline_target_stock=baseline_total,
        units_to_produce=units_to_produce,
        poll_interval=args.monitor_interval,
        max_wait_seconds=timeout,
    )


if __name__ == "__main__":
    main()
