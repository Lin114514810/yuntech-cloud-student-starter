#!/usr/bin/env python3
"""Check one W6 IoT identity by publishing or subscribing to a topic."""
import argparse
import json
import sys
import threading
import time

try:
    from .mqtt_common import (
        configure_tls,
        configured_code,
        identity_settings,
        mqtt_client,
        reason_value,
    )
except ImportError:
    from mqtt_common import (
        configure_tls,
        configured_code,
        identity_settings,
        mqtt_client,
        reason_value,
    )


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as", dest="identity", choices=("gw", "consumer"), required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--pub", metavar="TOPIC")
    action.add_argument("--sub", metavar="TOPIC_FILTER")
    parser.add_argument("--wait", type=float, default=60)
    args = parser.parse_args()
    if args.wait <= 0:
        parser.error("--wait must be greater than zero")
    return args


def run_check(args):
    code = configured_code()
    settings = identity_settings(args.identity, code)
    mqtt, client, ssl = mqtt_client(settings["client_id"])
    configure_tls(client, settings, ssl)

    connack_event = threading.Event()
    suback_event = threading.Event()
    puback_event = threading.Event()
    publish_lock = threading.Lock()
    state = {
        "connack": None,
        "pending_mid": None,
        "suback": None,
        "local_error": False,
    }

    def on_connect(connection, userdata, flags, reason_code, properties):
        code_value = reason_value(reason_code)
        state["connack"] = code_value
        print(f"connack={code_value}", flush=True)
        connack_event.set()
        if code_value >= 128:
            return
        if args.sub:
            result, _ = connection.subscribe(args.sub, qos=1)
            if result != mqtt.MQTT_ERR_SUCCESS:
                state["local_error"] = True
                print(f"subscribe_error rc={result}", file=sys.stderr, flush=True)
                suback_event.set()
        else:
            payload = json.dumps(
                {"check": "t2", "code": configured_code()},
                separators=(",", ":"),
            )
            with publish_lock:
                info = connection.publish(args.pub, payload, qos=1)
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    state["local_error"] = True
                    print(f"publish_error rc={info.rc}", file=sys.stderr, flush=True)
                    puback_event.set()
                else:
                    state["pending_mid"] = info.mid

    def on_subscribe(connection, userdata, mid, reason_codes, properties):
        result = reason_value(reason_codes[0]) if reason_codes else 128
        state["suback"] = result
        print(f"suback={result}", flush=True)
        suback_event.set()

    def on_publish(connection, userdata, mid, reason_code, properties):
        with publish_lock:
            if mid == state["pending_mid"]:
                print(f"puback={reason_value(reason_code)}", flush=True)
                puback_event.set()

    def on_message(connection, userdata, message):
        try:
            payload = message.payload.decode("utf-8")
        except UnicodeDecodeError:
            payload = repr(message.payload)
        print(f"recv topic={message.topic} payload={payload}", flush=True)

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_publish = on_publish
    client.on_message = on_message

    try:
        client.connect(settings["endpoint"], port=8883, keepalive=60)
        client.loop_start()
        if not connack_event.wait(15):
            raise RuntimeError("Timed out waiting for MQTT CONNACK")
        if state["connack"] is None or state["connack"] >= 128:
            return 1
        if args.pub:
            if not puback_event.wait(15):
                raise RuntimeError("Timed out waiting for MQTT PUBACK")
            return int(state["local_error"])
        started = time.monotonic()
        if not suback_event.wait(min(args.wait, 15)):
            raise RuntimeError("Timed out waiting for MQTT SUBACK")
        if state["local_error"]:
            return 1
        if suback_event.is_set() and state["suback"] is not None and state["suback"] < 128:
            remaining = args.wait - (time.monotonic() - started)
            if remaining > 0 and state["connack"] < 128:
                time.sleep(remaining)
        return 0
    finally:
        client.disconnect()
        client.loop_stop()


def main():
    args = parse_args()
    try:
        return run_check(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
