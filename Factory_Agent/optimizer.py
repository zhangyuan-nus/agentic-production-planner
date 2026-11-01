"""
Factory production optimizer.

Given a production goal (part, quantity, optional deadline, and an optional
"use existing stock" flag), analyses live factory snapshots and computes
which machines to enable, estimated completion time (in simulation seconds),
and bottleneck stages.

The model is a simplified view of the simulator:
- 1 simulation timestep = 1 second.
- Each active machine contributes a rate based on its CURRENT STATE
  (``STATE_PROD_RATE`` matches the simulator's ``machineStateProdMapping``:
  state 0 -> 10/s, 1 -> 9, 2 -> 8, 3 -> 5, 4-9 -> 0).
- A machine we recommend to enable is assumed to start at state 0, so it
  contributes ``NEW_ENABLE_RATE`` (= 10/s) until it degrades.
- Dependencies are expanded via PART_DEPENDENCIES.
- If ``use_existing_stock`` is set on the goal, container levels and lorry
  cargo are read from the snapshot and subtracted top-down from per-stage
  demand.
- Enabling a disabled machine incurs STARTUP_SECONDS ramp-up (reported as a
  warning, not subtracted from capacity).
- Cross-factory transport latency for lorry-eligible parts is flagged as a
  warning rather than modeled numerically.
- Internal transport caps (`m_c_move`/`c_m_move` at 1 unit/timestep) may
  limit sustained throughput below the headline rate; flagged as a warning.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from .config import (
    ALLOWED_MACHINE_IDS,
    LORRY_PARTS,
    MACHINE_TO_FACTORY,
    NEW_ENABLE_RATE,
    PART_DEPENDENCIES,
    PART_TO_MACHINE,
    STARTUP_SECONDS,
    STATE_PROD_RATE,
)
from .models.production_goal import (
    MachineRecommendation,
    OptimizationPlan,
    ProductionGoal,
    StageAnalysis,
)
from .snapshot import extract_stocks


def _transitive_dependencies(target: str) -> List[str]:
    """Return *target* plus every upstream part in dependency order (leaves first)."""
    visited: Set[str] = set()
    order: List[str] = []

    def _dfs(part: str) -> None:
        if part in visited:
            return
        visited.add(part)
        for dep in PART_DEPENDENCIES.get(part, []):
            _dfs(dep)
        order.append(part)

    _dfs(target)
    return order


def _snapshot_machine_key(machine_id: str) -> List[str]:
    """Return possible snapshot key names for a machine ID like 'F1M1' or 'F1M1b'."""
    m = re.fullmatch(r"F(\d+)M(\d+)(b?)", machine_id)
    if not m:
        return [machine_id]
    num = m.group(2)
    suffix = m.group(3)
    return [
        f"Machine {num}{suffix}",
        f"Machine {num}{suffix.upper()}",
        machine_id,
    ]


def _machine_state(snapshots: dict, factory_id: str, machine_id: str) -> str:
    """Read machine state from snapshot; returns '-1' when unknown/missing."""
    snap = snapshots.get(factory_id)
    if not isinstance(snap, dict):
        return "-1"
    machines = snap.get("machines_information", {})
    if not isinstance(machines, dict):
        return "-1"
    for key in _snapshot_machine_key(machine_id):
        info = machines.get(key)
        if isinstance(info, dict) and "state" in info:
            return str(info["state"]).strip()
    return "-1"


def _is_active(state: str) -> bool:
    """True if the machine is enabled (not disabled and not forced down)."""
    return state not in ("-1", "-2")


def _rate_for_state(state: str) -> float:
    """Map a machine's snapshot state string to its current nominal rate.

    Matches the simulator's `machineStateProdMapping`:
      * 0 -> 10/step, 1 -> 9, 2 -> 8, 3 -> 5
      * 4-7 (maintenance), 8-9 (failure), -1 (disabled), -2 (down) -> 0
    """
    try:
        s = int(state)
    except (TypeError, ValueError):
        return 0.0
    return float(STATE_PROD_RATE.get(s, 0.0))


def _stage_time_seconds(required: int, capacity: float) -> float:
    if required <= 0:
        return 0.0
    if capacity <= 0:
        return float("inf")
    return required / capacity


def _compute_net_demand(
    target: str,
    quantity: int,
    stocks: Dict[str, int],
) -> tuple[Dict[str, int], Dict[str, int]]:
    """Propagate demand top-down through the dependency graph.

    For a 1:1 BOM, each unit of a parent part consumes one unit of each of its
    direct dependencies. Stock at any level reduces how much of that part must
    be produced, which in turn reduces demand on its upstream parts.

    Returns ``(net_demand, stock_used)``:
      * ``net_demand[part]``  = units that must still be produced at that stage.
      * ``stock_used[part]``  = units of existing stock actually consumed to
        cover the order (at most ``quantity`` at the target, at most ``gross``
        at intermediates).
    """
    net_demand: Dict[str, int] = {}
    stock_used: Dict[str, int] = {}

    def _expand(part: str, gross_required: int) -> None:
        if gross_required <= 0:
            net_demand.setdefault(part, 0)
            return
        have = int(stocks.get(part, 0))
        used = min(have, gross_required)
        if used > 0:
            stock_used[part] = stock_used.get(part, 0) + used
        net = gross_required - used
        net_demand[part] = net_demand.get(part, 0) + net
        if net <= 0:
            return
        for dep in PART_DEPENDENCIES.get(part, []):
            _expand(dep, net)

    _expand(target, quantity)

    # Make sure every part in the chain has an entry (even if zero).
    for p in _transitive_dependencies(target):
        net_demand.setdefault(p, 0)

    return net_demand, stock_used


def compute_plan(goal: ProductionGoal, snapshots: dict) -> OptimizationPlan:
    warnings: List[str] = []

    if goal.target_part not in PART_TO_MACHINE:
        return OptimizationPlan(
            goal=goal,
            feasible=False,
            stages=[],
            recommendations=[],
            warnings=[f"Unknown part '{goal.target_part}'. Valid: {sorted(PART_TO_MACHINE.keys())}"],
        )

    required_parts = _transitive_dependencies(goal.target_part)
    stages: List[StageAnalysis] = []
    recommendations: List[MachineRecommendation] = []

    # --- stock lookup (only when requested) ---
    stocks: Dict[str, int] = {}
    if goal.use_existing_stock:
        stocks = extract_stocks(snapshots)
        if not stocks:
            warnings.append(
                "use_existing_stock was requested but no container stock data "
                "was found in the snapshots. Planning as if stock = 0."
            )

    # --- propagate demand through the BOM, top-down ---
    net_demand, stocks_used = _compute_net_demand(
        goal.target_part, goal.quantity, stocks
    )

    # --- build per-stage state ---
    for part in required_parts:
        machine_num = PART_TO_MACHINE[part]
        factory_id = MACHINE_TO_FACTORY.get(machine_num)

        stock_for_part = int(stocks.get(part, 0))
        required = int(net_demand.get(part, 0))

        if factory_id is None:
            warnings.append(f"No factory mapped for machine {machine_num} (part {part}).")
            stages.append(StageAnalysis(
                part=part,
                machine_number=machine_num,
                factory_id="?",
                capacity_per_second=0,
                active_machines=[],
                idle_machines=[],
                existing_stock=stock_for_part,
                required_units=required,
                estimated_stage_seconds=_stage_time_seconds(required, 0),
            ))
            continue

        primary_id = f"F{factory_id}M{machine_num}"
        backup_id = f"F{factory_id}M{machine_num}b"
        primary_state = _machine_state(snapshots, factory_id, primary_id)
        backup_state = _machine_state(snapshots, factory_id, backup_id)

        active: List[str] = []
        idle: List[str] = []
        capacity = 0.0

        if _is_active(primary_state):
            active.append(primary_id)
            capacity += _rate_for_state(primary_state)
        else:
            idle.append(primary_id)
        if _is_active(backup_state):
            active.append(backup_id)
            capacity += _rate_for_state(backup_state)
        else:
            idle.append(backup_id)

        stages.append(StageAnalysis(
            part=part,
            machine_number=machine_num,
            factory_id=factory_id,
            capacity_per_second=capacity,
            active_machines=active,
            idle_machines=idle,
            existing_stock=stock_for_part,
            required_units=required,
            estimated_stage_seconds=_stage_time_seconds(required, capacity),
        ))

    # --- greedy: enable idle machines at the slowest (longest time) stage first ---
    # Stages with 0 demand (stock already covers it) are skipped.
    changed = True
    while changed:
        changed = False
        stages_sorted = sorted(
            [s for s in stages if s.required_units > 0],
            key=lambda s: (-s.estimated_stage_seconds, s.capacity_per_second),
        )
        for stage in stages_sorted:
            if not stage.idle_machines:
                continue
            mid = stage.idle_machines.pop(0)
            if mid not in ALLOWED_MACHINE_IDS:
                continue
            # Enabling sets the machine's state to 0, so it produces at the
            # state-0 rate (until degradation via Markov transitions).
            rate = NEW_ENABLE_RATE
            stage.active_machines.append(mid)
            stage.capacity_per_second += rate
            stage.estimated_stage_seconds = _stage_time_seconds(
                stage.required_units, stage.capacity_per_second
            )
            recommendations.append(MachineRecommendation(
                machine_id=mid,
                factory_id=stage.factory_id,
                part=stage.part,
                action="enable",
                added_capacity=rate,
            ))
            changed = True
            break  # re-sort after each enablement

    # --- trim redundant recommendations ---
    # The greedy loop drains every idle machine, which over-provisions stages
    # that finish well before the global bottleneck. Walk the recommendations
    # and drop any whose removal still keeps the stage at-or-below the current
    # bottleneck time. This preserves the plan's completion time while
    # reducing the number of machines we actually turn on.
    active_stages_pre_trim = [s for s in stages if s.required_units > 0]
    if active_stages_pre_trim and recommendations:
        bottleneck_time = max(
            s.estimated_stage_seconds for s in active_stages_pre_trim
        )
        stage_by_part = {s.part: s for s in stages}

        # Fixpoint: keep dropping until no more can be safely removed.
        # Iteration order: try the LAST-added recommendations first, since
        # those were added to stages that were already fast.
        trimmed = 0
        while True:
            dropped_one = False
            for rec in reversed(list(recommendations)):
                stage = stage_by_part.get(rec.part)
                if stage is None:
                    continue
                new_capacity = stage.capacity_per_second - rec.added_capacity
                if new_capacity <= 0:
                    continue  # would leave the stage with no producer
                new_time = _stage_time_seconds(stage.required_units, new_capacity)
                # Allow tiny float slack so we don't miss exact-match removals.
                if new_time <= bottleneck_time + 1e-6:
                    stage.capacity_per_second = new_capacity
                    stage.estimated_stage_seconds = new_time
                    if rec.machine_id in stage.active_machines:
                        stage.active_machines.remove(rec.machine_id)
                    # Return the machine to the idle pool so later trim
                    # iterations see it correctly.
                    if rec.machine_id not in stage.idle_machines:
                        stage.idle_machines.append(rec.machine_id)
                    recommendations.remove(rec)
                    trimmed += 1
                    dropped_one = True
                    break
            if not dropped_one:
                break

        if trimmed:
            warnings.append(
                f"Trim pass removed {trimmed} redundant machine enable(s); "
                "the remaining set still meets the bottleneck time."
            )

    # --- identify bottleneck (longest per-stage time among stages that need production) ---
    active_stages = [s for s in stages if s.required_units > 0]
    if active_stages:
        slowest = max(s.estimated_stage_seconds for s in active_stages)
        bottleneck_part = next(
            (s.part for s in active_stages if s.estimated_stage_seconds == slowest),
            None,
        )
        for s in stages:
            s.is_bottleneck = (s.required_units > 0 and s.estimated_stage_seconds == slowest)
        estimated_seconds = slowest
        min_throughput = min(
            (s.capacity_per_second for s in active_stages),
            default=0.0,
        )
    else:
        bottleneck_part = None
        estimated_seconds = 0.0
        min_throughput = 0.0

    # --- special cases ---
    if active_stages and any(s.capacity_per_second <= 0 for s in active_stages):
        return OptimizationPlan(
            goal=goal,
            feasible=False,
            bottleneck_part=bottleneck_part,
            max_throughput_per_second=0,
            stages=stages,
            recommendations=recommendations,
            warnings=warnings + [
                "Zero capacity at one or more stages even after enabling all machines."
            ],
            stocks_used=stocks_used,
        )

    if not active_stages:
        warnings.append("Existing stock already covers the requested quantity at every stage. No production needed.")

    # --- startup-time awareness ---
    if recommendations:
        warnings.append(
            f"{len(recommendations)} machine(s) will be newly enabled; each incurs "
            f"~{STARTUP_SECONDS}s of startup ramp-up before producing."
        )

    # --- transport-cap caveat for combination stages ---
    combiner_stages = [
        s.part for s in active_stages
        if PART_DEPENDENCIES.get(s.part)  # has upstream deps -> combination stage
    ]
    if combiner_stages:
        warnings.append(
            "Combination stage(s) "
            f"{', '.join(combiner_stages)} may be feed-limited by the "
            "simulator's c_m_move (1 unit/timestep per input); actual sustained "
            "throughput can be below the headline per-state rate."
        )

    # --- cross-factory transport awareness ---
    transport_parts = [
        s.part for s in active_stages
        if s.part in LORRY_PARTS and s.part != goal.target_part
    ]
    if transport_parts:
        warnings.append(
            "Plan depends on lorry transport for part(s): "
            f"{', '.join(transport_parts)}. Travel time between factories is not "
            "included in estimated_seconds."
        )

    # --- deadline feasibility (seconds) ---
    deadline_met: Optional[bool] = None
    if goal.deadline is not None:
        now = datetime.now(timezone.utc)
        available_seconds = (goal.deadline - now).total_seconds()
        deadline_met = estimated_seconds <= available_seconds
        if not deadline_met:
            shortfall = estimated_seconds - available_seconds
            warnings.append(
                f"Deadline cannot be met. Need ~{estimated_seconds:.1f}s but only "
                f"{available_seconds:.1f}s available (short by {shortfall:.1f}s). "
                "Consider increasing machine rates or reducing quantity."
            )

    return OptimizationPlan(
        goal=goal,
        feasible=True,
        estimated_seconds=round(estimated_seconds, 2),
        deadline_met=deadline_met,
        bottleneck_part=bottleneck_part,
        max_throughput_per_second=round(min_throughput, 2) if active_stages else 0.0,
        stages=stages,
        recommendations=recommendations,
        warnings=warnings,
        stocks_used=stocks_used,
    )
