#!/usr/bin/env python3
"""Publish one deterministic 32-message W6 gateway reconciliation batch."""
import argparse
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone

try:
    from .mqtt_common import (
        CODE_PATTERN,
        configure_tls,
        configured_code,
        identity_settings,
        mqtt_client,
        reason_value,
    )
except ImportError:
    from mqtt_common import (
        CODE_PATTERN,
        configure_tls,
        configured_code,
        identity_settings,
        mqtt_client,
        reason_value,
    )

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAST_RUN_PATH = os.path.join(ROOT, ".local", "w06-last-run.json")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()
    if not CODE_PATTERN.fullmatch(args.code):
        parser.error("--code must match ^[A-Za-z0-9-]{1,40}$")
    if not CODE_PATTERN.fullmatch(args.run):
        parser.error("--run must match ^[A-Za-z0-9-]{1,40}$")
    if len(f"{args.code}-{args.run}") > 40:
        parser.error("combined run prefix must be at most 40 characters")
    return args


def write_last_run(prefix, puback_ok):
    directory = os.path.dirname(LAST_RUN_PATH)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".w06-last-run-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"prefix": prefix, "puback_ok": puback_ok}, stream)
            stream.write("\n")
        os.replace(temp_path, LAST_RUN_PATH)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def make_events(code, run):
    prefix = f"{code}-{run}"
    events = []
    first_event = None
    for device_number in range(1, 11):
        device_id = f"{code}-d{device_number:02d}"
        for sequence in range(1, 4):
            event = {
                "event_id": f"{prefix}-d{device_number:02d}-{sequence:02d}",
                "device_id": device_id,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "type": "status",
            }
            events.append(event)
            if first_event is None:
                first_event = event
    events.append(dict(first_event))
    events.append(
        {
            "event_id": f"{prefix}-d99-01",
            "device_id": f"{code}-d99",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "type": "status",
        }
    )
    return prefix, events


def publish_batch(code, run):
    if configured_code() != code:
        raise RuntimeError("The requested code does not match .local/resources.json")
    settings = identity_settings("gw", code)
    mqtt, client, ssl = mqtt_client(settings["client_id"])
    configure_tls(client, settings, ssl)

    connected = threading.Event()
    acked = threading.Event()
    state = {"connack": None, "mid": None, "reason": None}
    lock = threading.Lock()
    prefix, events = make_events(code, run)
    puback_ok = 0

    def on_connect(connection, userdata, flags, reason_code, properties):
        state["connack"] = reason_value(reason_code)
        connected.set()

    def on_publish(connection, userdata, mid, reason_code, properties):
        with lock:
            if mid == state["mid"]:
                state["reason"] = reason_value(reason_code)
                acked.set()

    client.on_connect = on_connect
    client.on_publish = on_publish
    try:
        client.connect(settings["endpoint"], port=8883, keepalive=60)
        client.loop_start()
        if not connected.wait(15):
            raise RuntimeError("Timed out waiting for MQTT CONNACK")
        if state["connack"] is None or state["connack"] >= 128:
            raise RuntimeError(f"MQTT connection rejected rc={state['connack']}")

        for event in events:
            payload = json.dumps(event, separators=(",", ":"))
            acked.clear()
            with lock:
                state["mid"] = None
                state["reason"] = None
                info = client.publish(settings["topic"], payload, qos=1)
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError(f"MQTT publish failed rc={info.rc}")
                state["mid"] = info.mid
            if not acked.wait(15):
                raise RuntimeError("Timed out waiting for MQTT PUBACK")
            if state["reason"] is None or state["reason"] >= 128:
                raise RuntimeError(f"MQTT PUBACK rejected rc={state['reason']}")
            puback_ok += 1
        return prefix, puback_ok
    finally:
        try:
            write_last_run(prefix, puback_ok)
        finally:
            client.disconnect()
            client.loop_stop()


def main():
    args = parse_args()
    try:
        prefix, count = publish_batch(args.code, args.run)
        print(f"puback_ok={count}/32")
        print(f"prefix={prefix}")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
