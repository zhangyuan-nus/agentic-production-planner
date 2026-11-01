from pathlib import Path

import pytest

from production_planner.config import FACTORY_MACHINES, MACHINE_TO_FACTORY, PART_TO_MACHINE
from production_planner.snapshot import load_snapshots

EXAMPLE_SNAPSHOT = Path(__file__).resolve().parents[1] / "examples" / "snapshot.json"


@pytest.fixture
def example_snapshot_path() -> Path:
    return EXAMPLE_SNAPSHOT


@pytest.fixture
def example_snapshots() -> dict[str, dict]:
    return load_snapshots(EXAMPLE_SNAPSHOT)


@pytest.fixture
def make_snapshots():
    """Build snapshots in the testbed's payload format.

    Every machine is disabled (-1) unless ``states`` gives it a state, e.g.
    ``{"F3M5b": -2}``. ``stock`` puts parts in a container at the factory that makes them.
    """

    def _make(states: dict[str, int] | None = None, stock: dict[str, int] | None = None):
        states = states or {}
        snapshots = {}
        for factory, machines in FACTORY_MACHINES.items():
            fid = factory.split()[-1]
            snapshots[fid] = {
                "machines_information": {
                    f"Machine {number}{suffix}": {
                        "state": states.get(f"F{fid}M{number}{suffix}", -1)
                    }
                    for number in machines
                    for suffix in ("", "b")
                },
                "container_information": {},
            }
        for part, amount in (stock or {}).items():
            fid = MACHINE_TO_FACTORY[PART_TO_MACHINE[part]]
            snapshots[fid]["container_information"][f"Container {part.upper()}"] = {
                "part": part.upper(),
                "amount": amount,
            }
        return snapshots

    return _make
