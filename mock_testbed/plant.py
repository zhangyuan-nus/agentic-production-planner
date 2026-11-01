"""SimPy model of the plant. No MQTT here, so it can be driven directly from tests."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

import simpy

# Machine number -> (factory, part made, parts consumed). Every machine has a backup
# with a "b" suffix that makes the same part.
MACHINES: dict[int, tuple[str, str, tuple[str, ...]]] = {
    1: ("1", "P1", ()),
    2: ("2", "P2", ()),
    3: ("2", "P12", ("P1", "P2")),
    4: ("3", "P3", ()),
    5: ("3", "P123", ("P12", "P3")),
    6: ("3", "P23", ("P2", "P3")),
    7: ("4", "P4", ()),
    8: ("4", "P234", ("P23", "P4")),
}

# Part -> (factory, container) that reports its stock, using the testbed's container names.
# The mock keeps one pool per part, so nothing travels between factories.
CONTAINERS: dict[str, tuple[str, str]] = {
    "P1": ("1", "Container 1"),
    "P2": ("2", "Container 3"),
    "P12": ("2", "Container 4"),
    "P3": ("3", "Container 6"),
    "P123": ("3", "Container A"),
    "P23": ("3", "Container 8"),
    "P4": ("4", "Container 10"),
    "P234": ("4", "Container B"),
}

FACTORIES = ("1", "2", "3", "4")
LORRY_PARTS = ("P1", "P2", "P12", "P23")
RATE_BY_STATE = {0: 10, 1: 9, 2: 8, 3: 5}  # units per second; states 4-9 produce nothing
STARTUP_SECONDS = 10
DISABLED = -1
FORCED_DOWN = -2


@dataclass
class Machine:
    factory: str
    label: str  # snapshot key, e.g. "Machine 5b"
    part: str
    inputs: tuple[str, ...]
    state: int = DISABLED
    startup_left: int = 0
    produced: int = 0

    @property
    def machine_id(self) -> str:
        return f"F{self.factory}M{self.label.split()[-1]}"


class Plant:
    """Four factories that advance one simulated second at a time."""

    def __init__(self, env: simpy.Environment, degrade: float = 0.0, seed: int | None = None):
        self.env = env
        self.degrade = degrade
        self._rng = random.Random(seed)
        self.stock: dict[str, int] = dict.fromkeys(CONTAINERS, 0)
        self.machines: dict[str, Machine] = {}
        for number, (factory, part, inputs) in MACHINES.items():
            for suffix in ("", "b"):
                machine = Machine(factory, f"Machine {number}{suffix}", part, inputs)
                self.machines[machine.machine_id] = machine
        env.process(self._run())

    def seed(self, snapshots: dict[str, dict]) -> None:
        """Take machine states and stock from saved ``Factory/<n>/Information`` payloads."""
        self.stock = dict.fromkeys(CONTAINERS, 0)
        for factory, snapshot in snapshots.items():
            for label, info in snapshot.get("machines_information", {}).items():
                machine = self.machines.get(f"F{factory}M{label.split()[-1]}")
                if machine is not None:
                    machine.state = int(info.get("state", DISABLED))
            for info in snapshot.get("container_information", {}).values():
                self._add_stock(info.get("part"), info.get("amount", 0))
            for cargo in snapshot.get("lorry_cargo", {}).values():
                for part in LORRY_PARTS:
                    self._add_stock(part, cargo.get(part, 0))

    def apply(self, payload: dict) -> list[str]:
        """Apply an ``optimizer/instructions`` message the way the testbed does.

        Enabling a disabled machine puts it in state 0 behind a fresh start-up delay, and
        enabling one that is already on does nothing. As in the testbed, an enable also
        revives a machine that was forced down, so the planner must never send one.
        Returns one log line per action.
        """
        if payload.get("type") not in ("optimization_plan", "optimization_plan_stop"):
            return [f"ignored message of type {payload.get('type')!r}"]
        log = []
        for action in payload.get("actions", []):
            machine_id = action.get("machine_id")
            machine = self.machines.get(machine_id)
            if machine is None or machine.factory != str(action.get("factory_id")):
                log.append(f"{machine_id}: unknown machine, skipped")
            elif action.get("action") == "enable":
                if machine.state >= 0:
                    log.append(f"{machine_id}: already enabled")
                else:
                    machine.state = 0
                    machine.startup_left = STARTUP_SECONDS
                    log.append(f"{machine_id}: enabled, making {machine.part} "
                               f"after {STARTUP_SECONDS}s start-up")
            elif action.get("action") == "disable":
                if machine.state == DISABLED:
                    log.append(f"{machine_id}: already disabled")
                else:
                    machine.state = DISABLED
                    machine.startup_left = 0
                    log.append(f"{machine_id}: disabled")
            else:
                log.append(f"{machine_id}: unknown action {action.get('action')!r}, skipped")
        return log

    def set_forced_down(self, machine_id: str, down: bool) -> str:
        """Mirror the testbed's ``<machine id> down`` topic."""
        machine = self.machines.get(machine_id)
        if machine is None:
            return f"{machine_id}: unknown machine"
        if down:
            machine.state = FORCED_DOWN
            machine.startup_left = 0
            return f"{machine_id}: forced down"
        machine.state = 0
        machine.startup_left = STARTUP_SECONDS
        return f"{machine_id}: back up, making {machine.part} after {STARTUP_SECONDS}s start-up"

    def snapshot(self, factory: str) -> dict:
        """The ``Factory/<n>/Information`` payload for one factory."""
        return {
            "time": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "sim_time": int(self.env.now),
            "machines_information": {
                m.label: {"state": m.state, "totalPartsProduced": m.produced}
                for m in self.machines.values()
                if m.factory == factory
            },
            "container_information": {
                name: {"part": part, "amount": self.stock[part]}
                for part, (owner, name) in CONTAINERS.items()
                if owner == factory
            },
            "lorry_cargo": {},
        }

    def status(self) -> str:
        running = [m.machine_id for m in self.machines.values() if m.state >= 0]
        stock = " ".join(f"{part}={amount}" for part, amount in self.stock.items() if amount)
        return (f"t={self.env.now:.0f}s  on: {', '.join(running) or 'none'}  "
                f"stock: {stock or 'empty'}")

    def _add_stock(self, part, amount) -> None:
        if part in self.stock:
            self.stock[part] += int(amount)

    def _run(self):
        while True:
            yield self.env.timeout(1)
            self._step()

    def _step(self) -> None:
        # Machines are listed in BOM order, so a part made this second can feed the next stage.
        for machine in self.machines.values():
            if machine.state < 0:
                continue
            if machine.startup_left > 0:
                machine.startup_left -= 1
                continue
            units = RATE_BY_STATE.get(machine.state, 0)
            for part in machine.inputs:
                units = min(units, self.stock[part])
            for part in machine.inputs:
                self.stock[part] -= units
            self.stock[machine.part] += units
            machine.produced += units
            if units and machine.state < 3 and self._rng.random() < self.degrade:
                machine.state += 1
