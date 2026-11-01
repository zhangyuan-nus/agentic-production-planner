"""Run the mock testbed against an MQTT broker.

    python -m mock_testbed                                         # every machine off, no stock
    python -m mock_testbed --from-snapshot examples/snapshot.json  # start from a saved state
    mosquitto_pub -t "F3M5 down" -m true                           # force a machine down
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys

import paho.mqtt.client as mqtt
from dotenv import find_dotenv, load_dotenv
from simpy.rt import RealtimeEnvironment

from .plant import FACTORIES, Plant

INSTRUCTIONS_TOPIC = "optimizer/instructions"
STATUS_EVERY_SECONDS = 10


def handle_message(plant: Plant, topic: str, payload: bytes) -> list[str]:
    if topic == INSTRUCTIONS_TOPIC:
        try:
            message = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ["ignored instructions that are not valid JSON"]
        return plant.apply(message)
    machine_id = topic.removesuffix(" down")
    down = payload.decode(errors="replace").strip().lower() in ("1", "true", "on", "yes")
    return [plant.set_forced_down(machine_id, down)]


def main(argv: list[str] | None = None) -> int:
    load_dotenv(find_dotenv(usecwd=True))
    parser = argparse.ArgumentParser(
        prog="mock_testbed",
        description="Simulate the four-factory testbed and bridge it to MQTT.",
    )
    parser.add_argument("--broker", default=os.getenv("MQTT_BROKER", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MQTT_PORT", "1883")))
    parser.add_argument("--speed", type=float, default=1.0,
                        help="simulated seconds per real second (default: 1)")
    parser.add_argument("--from-snapshot", metavar="FILE",
                        help="start from the machine states and stock in a saved snapshot")
    parser.add_argument("--degrade", type=float, default=0.0, metavar="P",
                        help="chance per second that a producing machine loses one health "
                             "state (default: 0)")
    parser.add_argument("--seed", type=int, help="random seed for --degrade")
    parser.add_argument("--duration", type=float, help="stop after this many simulated seconds")
    args = parser.parse_args(argv)
    if args.speed <= 0:
        parser.error("--speed must be positive")

    env = RealtimeEnvironment(factor=1 / args.speed, strict=False)
    plant = Plant(env, degrade=args.degrade, seed=args.seed)
    if args.from_snapshot:
        try:
            with open(args.from_snapshot, encoding="utf-8") as f:
                plant.seed(json.load(f))
        except (OSError, ValueError) as exc:
            print(f"[mock] Cannot read {args.from_snapshot}: {exc}", file=sys.stderr)
            return 2

    inbox: queue.Queue[tuple[str, bytes]] = queue.Queue()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    def on_connect(client, _userdata, _flags, _reason_code, _properties):
        client.subscribe(INSTRUCTIONS_TOPIC, qos=1)
        for machine_id in plant.machines:
            client.subscribe(f"{machine_id} down", qos=1)

    client.on_connect = on_connect
    client.on_message = lambda _client, _userdata, msg: inbox.put((msg.topic, msg.payload))
    try:
        client.connect(args.broker, args.port)
    except OSError as exc:
        print(f"[mock] Cannot reach the MQTT broker at {args.broker}:{args.port}: {exc}",
              file=sys.stderr)
        return 2
    client.loop_start()

    def bridge():
        while True:
            while not inbox.empty():
                topic, payload = inbox.get_nowait()
                for line in handle_message(plant, topic, payload):
                    print(f"[mock] t={env.now:.0f}s  {line}")
            for factory in FACTORIES:
                client.publish(f"Factory/{factory}/Information",
                               json.dumps(plant.snapshot(factory)))
            if env.now % STATUS_EVERY_SECONDS == 0:
                print(f"[mock] {plant.status()}")
            yield env.timeout(1)

    env.process(bridge())
    print(f"[mock] Publishing Factory/1-4/Information to {args.broker}:{args.port} every "
          f"simulated second at {args.speed:g}x speed. Ctrl-C to stop.")
    try:
        env.run(until=args.duration)
    except KeyboardInterrupt:
        print("\n[mock] Stopped.")
    finally:
        client.disconnect()
        client.loop_stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
