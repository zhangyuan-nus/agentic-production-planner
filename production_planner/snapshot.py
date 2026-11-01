"""Factory snapshots: collect them from MQTT (or a saved file) and aggregate stock.

Every factory in the testbed publishes a JSON snapshot on ``Factory/<id>/Information``
each simulated second. The planner only needs the latest message per factory.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .config import FACTORY_INFO_TOPIC, FACTORY_MACHINES
from .mqtt_io import connect


def factory_id_from_topic(topic: str) -> str | None:
    """Return ``"3"`` for ``Factory/3/Information``, or None for any other topic."""
    parts = str(topic).split("/")
    if len(parts) == 3 and parts[0] == "Factory" and parts[2] == "Information":
        return parts[1]
    return None


def get_all_factory_snapshots(timeout: float = 10) -> dict[str, dict]:
    """Subscribe to every factory's information topic and collect the latest message per factory.

    Returns as soon as all factories have reported, or after ``timeout`` seconds.
    """
    results: dict[str, dict] = {}

    def on_message(_client, _userdata, msg):
        fid = factory_id_from_topic(msg.topic)
        if fid is None:
            return
        try:
            results[fid] = json.loads(msg.payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            results[fid] = {
                "error": "bad_json",
                "raw": msg.payload.decode(errors="replace"),
            }

    client = connect()
    client.on_message = on_message
    client.subscribe(FACTORY_INFO_TOPIC, qos=1)
    client.loop_start()
    try:
        deadline = time.monotonic() + timeout
        while len(results) < len(FACTORY_MACHINES) and time.monotonic() < deadline:
            time.sleep(0.05)
        return dict(results)
    finally:
        client.disconnect()
        client.loop_stop()


def load_snapshots(path: str | Path) -> dict[str, dict]:
    """Load snapshots saved as ``{"<factory id>": <Information payload>, ...}``."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object keyed by factory id")
    return {str(fid): snap for fid, snap in data.items()}


_CONTAINER_KEYS = (
    "container_information",   # canonical key used by the simulator
    "containers_information",  # historical / alternative spellings
    "containers",
    "shop_containers",
)
_PART_KEYS = ("partContained", "part_contained", "part", "content")
_LEVEL_KEYS = ("amount", "level", "stock", "count", "size")
# Which parts can appear as lorry cargo, per simulator LORRY_PARTS.
_LORRY_CARGO_PARTS = ("P1", "P2", "P12", "P23")


def _coerce_int(value) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return 0
    if isinstance(value, list):
        return len(value)
    return 0


def _normalise_part(value) -> str:
    """Turn various part name spellings into canonical lowercase (e.g. 'P12' -> 'p12')."""
    if not isinstance(value, str):
        return ""
    s = value.strip().lower()
    m = re.fullmatch(r"p\s*([0-9]+)", s)
    if m:
        return f"p{m.group(1)}"
    return s


def extract_stocks(snapshots: dict) -> dict[str, int]:
    """Aggregate per-part inventory across all factories in the snapshot set.

    Sums:
      * container levels from ``container_information`` (shop-floor storage), and
      * lorry cargo from ``lorry_cargo`` (in-transit inventory for P1/P2/P12/P23).

    Returns ``{}`` if no inventory information is found in any snapshot.
    """
    totals: dict[str, int] = {}

    for snap in snapshots.values():
        if not isinstance(snap, dict):
            continue

        _aggregate_containers(snap, totals)
        _aggregate_lorry_cargo(snap, totals)

    return totals


def _aggregate_containers(snap: dict, totals: dict[str, int]) -> None:
    containers = None
    for key in _CONTAINER_KEYS:
        if isinstance(snap.get(key), dict):
            containers = snap[key]
            break
    if not containers:
        return

    for cname, cinfo in containers.items():
        if not isinstance(cinfo, dict):
            if isinstance(cinfo, (int, float, list, str)):
                part = _part_from_container_name(str(cname))
                if part:
                    totals[part] = totals.get(part, 0) + _coerce_int(cinfo)
            continue

        part = ""
        for pk in _PART_KEYS:
            if pk in cinfo:
                part = _normalise_part(cinfo[pk])
                if part:
                    break
        if not part:
            part = _part_from_container_name(str(cname))
        if not part:
            continue

        level = 0
        for lk in _LEVEL_KEYS:
            if lk in cinfo:
                level = _coerce_int(cinfo[lk])
                break

        totals[part] = totals.get(part, 0) + level


def _aggregate_lorry_cargo(snap: dict, totals: dict[str, int]) -> None:
    cargo = snap.get("lorry_cargo")
    if not isinstance(cargo, dict):
        return
    for info in cargo.values():
        if not isinstance(info, dict):
            continue
        for raw_part in _LORRY_CARGO_PARTS:
            if raw_part in info:
                part = _normalise_part(raw_part)
                qty = _coerce_int(info[raw_part])
                if qty > 0:
                    totals[part] = totals.get(part, 0) + qty


def _part_from_container_name(name: str) -> str:
    """Best-effort: extract a part code from a container name like 'F1C_P1' or 'P12 container'."""
    m = re.search(r"p\s*([0-9]+)", name, re.IGNORECASE)
    if m:
        return f"p{m.group(1)}"
    return ""
