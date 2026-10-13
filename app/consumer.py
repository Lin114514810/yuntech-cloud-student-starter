#!/usr/bin/env python3
"""Forward authorized IoT events to the local inspection API."""
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required setting: {name}")
    return value


def reason_value(reason_code):
    return int(getattr(reason_code, "value", reason_code))


def load_settings():
    device_ids = {
        value.strip()
        for value in required_env("IOT_DEVICE_IDS").split(",")
        if value.strip()
    }
    if not device_ids or any(
        not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value) for value in device_ids
    ):
        raise RuntimeError("IOT_DEVICE_IDS must contain valid comma-separated IDs")
    return {
        "endpoint": required_env("IOT_ENDPOINT"),
        "topic": required_env("IOT_TOPIC"),
        "client_id": required_env("IOT_CLIENT_ID"),
        "device_ids": device_ids,
        "ca": required_env("IOT_CA"),
        "cert": required_env("IOT_CERT"),
        "key": required_env("IOT_KEY"),
        "reporter_token": required_env("REPORTER_TOKEN"),
    }


def event_id_for_log(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        return value
    return "invalid"


def post_event(payload, reporter_token):
    request = urllib.request.Request(
        "http://127.0.0.1:8080/events",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {reporter_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
    except (urllib.error.URLError, TimeoutError, OSError):
        return "error"

    if status == 201:
        return "created"
    if status == 200:
        return "duplicate"
    if 400 <= status < 500:
        return "rejected"
    return "error"


def process_message(payload_bytes, settings):
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "invalid", "rejected"
    if not isinstance(payload, dict):
        return "invalid", "rejected"

    event_id = event_id_for_log(payload.get("event_id"))
    device_id = payload.get("device_id")
    if not isinstance(device_id, str) or device_id not in settings["device_ids"]:
        return event_id, "rejected"
    return event_id, post_event(payload, settings["reporter_token"])


def make_client(settings):
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise RuntimeError("Install paho-mqtt==2.1.0 before starting the consumer") from exc
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=settings["client_id"],
        protocol=mqtt.MQTTv5,
    )
    client.tls_set(
        ca_certs=settings["ca"],
        certfile=settings["cert"],
        keyfile=settings["key"],
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )

    def on_connect(connection, userdata, flags, reason_code, properties):
        code = reason_value(reason_code)
        if code >= 128:
            print(f"connect failed rc={code}", file=sys.stderr, flush=True)
            connection.disconnect()
            return
        connection.subscribe(settings["topic"], qos=1)

    def on_subscribe(connection, userdata, mid, reason_code_list, properties):
        code = reason_value(reason_code_list[0]) if reason_code_list else 128
        print(f"subscribed rc={code}", flush=True)
        if code >= 128:
            connection.disconnect()

    def on_message(connection, userdata, message):
        event_id, result = process_message(message.payload, settings)
        print(f"event_id={event_id} result={result}", flush=True)

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    return client


def main():
    try:
        settings = load_settings()
        client = make_client(settings)
        client.connect(settings["endpoint"], port=8883, keepalive=60)
        client.loop_forever()
    except (RuntimeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
