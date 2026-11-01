"""Small paho-mqtt helpers so every module talks to the broker the same way."""

import json

import paho.mqtt.client as mqtt

from .config import MQTT_BROKER, MQTT_PORT


def connect(client_id: str = "") -> mqtt.Client:
    """Return a client connected to the configured broker; the caller starts the loop."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    client.connect(MQTT_BROKER, MQTT_PORT)
    return client


def publish_json(topic: str, payload: dict, timeout: float = 5.0) -> None:
    """Publish ``payload`` as JSON (QoS 1) and wait until the broker has acknowledged it."""
    client = connect()
    client.loop_start()
    try:
        info = client.publish(topic, json.dumps(payload, indent=2), qos=1)
        info.wait_for_publish(timeout=timeout)
    finally:
        client.disconnect()
        client.loop_stop()
