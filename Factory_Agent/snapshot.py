"""MQTT snapshot helper for workflow 3.

Subscribes to Factory/+/Information and returns the latest JSON payload per
factory, keyed by factory id (e.g. "1", "2", ...). Also provides
``extract_stocks`` to aggregate container levels from those snapshots.
"""

from __future__ import annotations

import json
import re
import time
from typing import Dict

import paho.mqtt.client as mqtt

from .config import MQTT_BROKER


def get_all_factory_snapshots(timeout: int = 10) -> dict:
    """Subscribe to Factory/+/Information and collect the latest message per factory."""
    results: dict = {}

    def on_message(_client, _userdata, msg):
        parts = str(msg.topic).split("/")
        if len(parts) == 3 and parts[0] == "Factory" and parts[2] == "Information":
            fid = parts[1]
            try:
                results[fid] = json.loads(msg.payload.decode())
            except Exception:
                results[fid] = {
                    "error": "bad_json",
                    "raw": msg.payload.decode(errors="replace"),
                }

    c = mqtt.Client()
    c.on_message = on_message
    c.connect(MQTT_BROKER)
    c.subscribe("Factory/+/Information", qos=1)
    c.loop_start()

    try:
        start = time.time()
        while len(results) < 4 and (time.time() - start) < timeout:
            time.sleep(0.05)
        return results
    finally:
        c.loop_stop()
        c.disconnect()


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


def extract_stocks(snapshots: dict) -> Dict[str, int]:
    """Aggregate per-part inventory across all factories in the snapshot set.

    Sums:
      * container levels from ``container_information`` (shop-floor storage), and
      * lorry cargo from ``lorry_cargo`` (in-transit inventory for P1/P2/P12/P23).

    Returns ``{}`` if no inventory information is found in any snapshot.
    """
    totals: Dict[str, int] = {}

    for _fid, snap in snapshots.items():
        if not isinstance(snap, dict):
            continue

        _aggregate_containers(snap, totals)
        _aggregate_lorry_cargo(snap, totals)

    return totals


def _aggregate_containers(snap: dict, totals: Dict[str, int]) -> None:
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


def _aggregate_lorry_cargo(snap: dict, totals: Dict[str, int]) -> None:
    cargo = snap.get("lorry_cargo")
    if not isinstance(cargo, dict):
        return
    for _lorry_name, info in cargo.items():
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
