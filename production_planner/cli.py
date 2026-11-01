"""
Command-line entry point: order -> plan -> operator approval -> dispatch -> auto-stop.

    python -m production_planner --order "make 500 p123 by 6pm today, use existing stock"
    python -m production_planner --part p234 --qty 300 --deadline 2026-04-17T18:00
    python -m production_planner --part p123 --qty 500 --use-existing-stock \\
        --snapshot-file examples/snapshot.json --no-llm

Pipeline:
  1. (optional) The order agent turns free text into a ProductionGoal (Gemini).
  2. Live factory snapshots are read over MQTT (or from a saved file).
  3. The deterministic optimiser decides which machines to enable.
  4. (optional) Gemini summarises the plan for a plant manager.
  5. Nothing is published until the operator confirms.
  6. Stock is watched until the order is complete, then those machines are switched off.
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap

from pydantic import ValidationError

from .config import INSTRUCTIONS_TOPIC, MQTT_BROKER, MQTT_PORT, PART_TO_MACHINE, STARTUP_SECONDS
from .executor import build_instructions, monitor_until_complete, publish_instructions
from .models import OptimizationPlan, ProductionGoal
from .optimizer import compute_plan
from .order_agent import intent_to_goal, parse_deadline, parse_order, print_intent
from .snapshot import extract_stocks, get_all_factory_snapshots, load_snapshots
from .summary import summarise_plan

# Parses below this confidence always need an explicit go-ahead from the operator.
LOW_CONFIDENCE = 0.6


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="production_planner",
        description="Plan and dispatch production orders across the four-factory testbed.",
    )
    order = parser.add_argument_group("order (either --order, or --part and --qty)")
    order.add_argument("--order", help="natural-language order, parsed by the Gemini order agent")
    order.add_argument("--part", help=f"target part: {', '.join(sorted(PART_TO_MACHINE))}")
    order.add_argument("--qty", type=_positive_int, help="number of units to produce")
    order.add_argument(
        "--deadline", help="ISO-8601 deadline, e.g. 2026-04-17T18:00 (local time if no offset)"
    )
    order.add_argument(
        "--use-existing-stock",
        action="store_true",
        help="count current container and lorry stock against the order",
    )

    flow = parser.add_argument_group("workflow")
    flow.add_argument(
        "--snapshot-file",
        help="plan against a saved snapshot JSON instead of live MQTT (nothing is published)",
    )
    flow.add_argument(
        "--mqtt-timeout", type=float, default=10,
        help="seconds to wait for live factory snapshots (default: 10)",
    )
    flow.add_argument("--no-llm", action="store_true", help="skip the Gemini plan summary")
    flow.add_argument("--show-json", action="store_true", help="also print the raw plan JSON")
    flow.add_argument(
        "--confirm-order", action="store_true",
        help="with --order, confirm the parsed order before planning",
    )
    flow.add_argument(
        "--auto-confirm", action="store_true",
        help="publish the plan without asking for confirmation",
    )

    monitor = parser.add_argument_group("completion monitor")
    monitor.add_argument(
        "--no-monitor", action="store_true",
        help="do not watch for completion or switch machines off afterwards",
    )
    monitor.add_argument(
        "--monitor-interval", type=float, default=2.0,
        help="seconds between completion checks (default: 2)",
    )
    monitor.add_argument(
        "--monitor-timeout", type=float, default=0.0,
        help="give up after N seconds (default 0 = 2x the estimate, clamped to 5 min .. 24 h)",
    )
    return parser


def _ask(prompt: str) -> bool:
    while True:
        answer = input(prompt).strip().lower()
        if answer in ("y", "yes", ""):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please enter Y or N.")


def _goal_from_order(args: argparse.Namespace) -> ProductionGoal | None:
    """Run the order agent on ``--order``. Returns None if the operator cancels."""
    if args.part or args.qty is not None or args.deadline or args.use_existing_stock:
        print("[order-agent] NOTE: --order overrides --part / --qty / --deadline / "
              "--use-existing-stock.")
    print(f"[order-agent] Parsing: {args.order!r}\n")
    intent = parse_order(args.order)
    print_intent(intent)

    confirm = args.confirm_order
    if intent.confidence < LOW_CONFIDENCE:
        print(f"\n[order-agent] WARNING: low confidence ({intent.confidence:.2f}); "
              "check the parsed order carefully.")
        confirm = confirm or not args.auto_confirm
    if confirm and not _ask("\nProceed with this parsed order? [Y/n]: "):
        return None
    return intent_to_goal(intent)


def _goal_from_flags(args: argparse.Namespace, parser: argparse.ArgumentParser) -> ProductionGoal:
    if not args.part or args.qty is None:
        parser.error("give --order, or both --part and --qty")
    deadline = None
    if args.deadline:
        try:
            deadline = parse_deadline(args.deadline)
        except ValueError:
            parser.error(
                f"--deadline must be ISO-8601, e.g. 2026-04-17T18:00 (got {args.deadline!r})"
            )
    goal = ProductionGoal(
        target_part=args.part,
        quantity=args.qty,
        deadline=deadline,
        use_existing_stock=args.use_existing_stock,
    )
    if goal.target_part not in PART_TO_MACHINE:
        valid = ", ".join(sorted(PART_TO_MACHINE))
        parser.error(f"unknown part {args.part!r}; choose from {valid}")
    return goal


def _auto_timeout(estimated_seconds: float | None) -> float:
    """Twice the estimate, clamped to 5 minutes .. 24 hours (1 hour without an estimate)."""
    if not estimated_seconds:
        return 3600.0
    return max(300.0, min(24 * 3600.0, estimated_seconds * 2.0))


def print_plan(plan: OptimizationPlan) -> None:
    goal = plan.goal
    deadline = goal.deadline.isoformat(timespec="minutes") if goal.deadline else "none"
    print("\n===== PRODUCTION PLAN =====")
    print(f"  Order       : {goal.quantity} x {goal.target_part} (deadline: {deadline})")
    print(f"  Feasible    : {'yes' if plan.feasible else 'NO'}")
    if plan.estimated_seconds is not None:
        startup = f" + {STARTUP_SECONDS}s machine start-up" if plan.recommendations else ""
        print(f"  Estimate    : {plan.estimated_seconds:g}s of production{startup}")
    if plan.deadline_met is not None:
        print(f"  Deadline    : {'met' if plan.deadline_met else 'NOT met'}")
    if plan.bottleneck_part:
        print(f"  Bottleneck  : {plan.bottleneck_part}")
    stock = "not used"
    if goal.use_existing_stock:
        drawn = ", ".join(f"{part} x{qty}" for part, qty in sorted(plan.stocks_used.items()))
        stock = f"drawing {drawn}" if drawn else "requested, but none available"
    print(f"  Stock       : {stock}")

    if plan.stages:
        print()
        print(f"  {'Stage':<6} {'Factory':<8} {'Machines on':<13} {'Rate/s':>6} "
              f"{'Stock':>6} {'To make':>8} {'Time s':>7}")
        for s in plan.stages:
            machines = ", ".join(s.active_machines) or "-"
            seconds = s.estimated_stage_seconds
            seconds_text = "inf" if seconds == float("inf") else f"{seconds:.1f}"
            marker = "  <- bottleneck" if s.is_bottleneck else ""
            print(f"  {s.part:<6} {'F' + s.factory_id:<8} {machines:<13} "
                  f"{s.capacity_per_second:>6.1f} {s.existing_stock:>6} "
                  f"{s.required_units:>8} {seconds_text:>7}{marker}")

    if plan.recommendations:
        print(f"\n  Machine actions ({len(plan.recommendations)}):")
        for rec in plan.recommendations:
            print(f"    {rec.action.upper():<7} {rec.machine_id:<6} Factory {rec.factory_id}, "
                  f"part {rec.part} (+{rec.added_capacity:g}/s)")

    if plan.warnings:
        print("\n  Warnings:")
        for warning in plan.warnings:
            print(textwrap.fill(warning, width=92, initial_indent="    - ",
                                subsequent_indent="      ", break_on_hyphens=False))


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.order:
        try:
            goal = _goal_from_order(args)
        except (RuntimeError, ValueError, ValidationError) as exc:
            print(f"[order-agent] Failed to parse order: {exc}", file=sys.stderr)
            return 2
        if goal is None:
            print("[order-agent] Cancelled.")
            return 0
    else:
        goal = _goal_from_flags(args, parser)

    offline = args.snapshot_file is not None
    if offline:
        try:
            snapshots = load_snapshots(args.snapshot_file)
        except (OSError, ValueError) as exc:
            print(f"[planner] Cannot read snapshot file: {exc}", file=sys.stderr)
            return 2
        print(f"[planner] Loaded snapshots for factories {', '.join(sorted(snapshots))} "
              f"from {args.snapshot_file}")
    else:
        print(f"[planner] Reading live factory snapshots from {MQTT_BROKER}:{MQTT_PORT} "
              f"(timeout {args.mqtt_timeout:g}s) ...")
        try:
            snapshots = get_all_factory_snapshots(timeout=args.mqtt_timeout)
        except OSError as exc:
            print(f"[planner] Cannot reach the MQTT broker at {MQTT_BROKER}:{MQTT_PORT}: {exc}",
                  file=sys.stderr)
            return 2
        if snapshots:
            print(f"[planner] Snapshots received from factories: {', '.join(sorted(snapshots))}")
        else:
            print("[planner] WARNING: no snapshots received; planning as if every machine is off.")

    plan = compute_plan(goal, snapshots)
    print_plan(plan)
    if args.show_json:
        print("\n===== PLAN JSON =====")
        print(plan.model_dump_json(indent=2))

    if not args.no_llm:
        print("\n===== LLM SUMMARY =====")
        print(summarise_plan(plan))

    if not plan.feasible:
        print("\n[planner] Plan is not feasible. No actions to apply.")
        return 1
    if all(s.required_units <= 0 for s in plan.stages):
        print("\n[planner] Existing stock already covers the order. Nothing to produce.")
        return 0
    if not plan.recommendations:
        print("\n[planner] No machine changes needed. Current capacity is sufficient.")
        return 0

    instructions = build_instructions(plan)
    if offline:
        print(f"\n[planner] Offline mode: this payload would be published to "
              f"'{INSTRUCTIONS_TOPIC}':")
        print(json.dumps(instructions, indent=2))
        return 0

    prompt = f"\nApply these {len(plan.recommendations)} machine action(s)? [Y/n]: "
    if not (args.auto_confirm or _ask(prompt)):
        print("[planner] Cancelled. No instructions sent.")
        return 0

    publish_instructions(instructions)
    print(f"[planner] Published plan to '{INSTRUCTIONS_TOPIC}'.")

    if args.no_monitor:
        print("[planner] --no-monitor set: the machines stay on until a stop payload is sent.")
        return 0

    completed = monitor_until_complete(
        plan=plan,
        baseline_target_stock=extract_stocks(snapshots).get(goal.target_part, 0),
        units_to_produce=goal.quantity - plan.stocks_used.get(goal.target_part, 0),
        poll_interval=args.monitor_interval,
        max_wait_seconds=args.monitor_timeout or _auto_timeout(plan.estimated_seconds),
    )
    return 0 if completed else 1


if __name__ == "__main__":
    sys.exit(main())
