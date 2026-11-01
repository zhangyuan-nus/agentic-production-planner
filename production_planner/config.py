"""Runtime settings and the plant model shared by the planner modules."""

import os

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

MQTT_BROKER = os.getenv("MQTT_BROKER", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))

# Topics defined by the factory testbed.
FACTORY_INFO_TOPIC = "Factory/+/Information"
INSTRUCTIONS_TOPIC = "optimizer/instructions"

# Part -> primary machine number.
# Simulator layout:
#   F1: M1 creates p1
#   F2: M2 creates p2, M3 combines p1+p2 -> p12
#   F3: M4 creates p3, M5 combines p12+p3 -> p123, M6 combines p2+p3 -> p23
#   F4: M7 creates p4, M8 combines p23+p4 -> p234
PART_TO_MACHINE = {
    "p1": 1,
    "p2": 2,
    "p12": 3,
    "p3": 4,
    "p123": 5,
    "p23": 6,
    "p4": 7,
    "p234": 8,
}

# target part -> direct upstream parts (bill of materials edges)
PART_DEPENDENCIES = {
    "p1": [],
    "p2": [],
    "p3": [],
    "p4": [],
    "p12": ["p1", "p2"],
    "p23": ["p2", "p3"],
    "p123": ["p12", "p3"],
    "p234": ["p23", "p4"],
}

# parts that physically move between factories on lorries (ring F1->F2->F3->F4->F1)
# parts NOT in this set are produced and consumed locally
LORRY_PARTS = {"p1", "p2", "p12", "p23"}

FACTORY_MACHINES = {
    "Factory 1": [1],
    "Factory 2": [2, 3],
    "Factory 3": [4, 5, 6],
    "Factory 4": [7, 8],
}

# Canonical machine IDs the optimizer is allowed to toggle.
# Primary machines are what the simulator description documents; backups (suffix `b`)
# are included for deployments that provision them. Enablement of a non-existent
# backup is a no-op on the simulator side.
ALLOWED_MACHINE_IDS = {
    "F1M1", "F1M1b",
    "F2M2", "F2M2b", "F2M3", "F2M3b",
    "F3M4", "F3M4b", "F3M5", "F3M5b", "F3M6", "F3M6b",
    "F4M7", "F4M7b", "F4M8", "F4M8b",
}

# Per-state production rate, in UNITS PER SIMULATION TIMESTEP (= 1 second).
#
# Matches the simulator's `machineStateProdMapping` used by both
# `machine_create` and `machine_combine`:
#   state 0 -> 10 parts/step (fresh / healthy)
#   state 1 -> 9
#   state 2 -> 8
#   state 3 -> 5
#   state 4-7 (maintenance), 8-9 (failure) -> 0
# A machine re-enabled via `optimizer/instructions` starts at state 0.
STATE_PROD_RATE: dict[int, float] = {0: 10.0, 1: 9.0, 2: 8.0, 3: 5.0}

# Rate assumed for a machine we are about to enable (enable sets state=0).
NEW_ENABLE_RATE: float = STATE_PROD_RATE[0]

# NOTE: The simulator's internal transport (`m_c_move` and `c_m_move`) moves
# at most 1 unit per timestep per link. In long-running steady state this can
# cap realised throughput below the per-state machine rate, especially for
# combiners. The optimiser surfaces this as a plan warning rather than
# de-rating the numbers, so the headline capacity here is the machine's
# nominal rate at its current state.

# Simulation seconds between enabling a machine and it starting to produce.
# Applies every time a machine transitions from disabled -> enabled (including
# re-enabling after a fault). Used to warn the operator about ramp-up delay.
STARTUP_SECONDS = 10

MACHINE_TO_FACTORY: dict[int, str] = {}
for _fname, _machines in FACTORY_MACHINES.items():
    _fid = _fname.split()[-1]
    for _m in _machines:
        MACHINE_TO_FACTORY[_m] = _fid
