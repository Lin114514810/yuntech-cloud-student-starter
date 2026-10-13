"""Shared local identity and MQTT setup for W6 tools."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL_IOT = ROOT / ".local" / "iot"
CODE_PATTERN = re.compile(r"[A-Za-z0-9-]{1,40}\Z")


def read_resources():
    path = ROOT / ".local" / "resources.json"
    try:
        resources = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Cannot read .local/resources.json") from exc
    if not isinstance(resources, dict):
        raise RuntimeError(".local/resources.json must contain an object")
    return resources


def configured_code(resources=None):
    resources = resources if resources is not None else read_resources()
    code = resources.get("iot_code")
    if not code:
        policy = resources.get("iot_gw_policy", "")
        match = re.fullmatch(r"cxe-([A-Za-z0-9-]{1,40})-gw", policy)
        code = match.group(1) if match else None
    if not isinstance(code, str) or not CODE_PATTERN.fullmatch(code):
        raise RuntimeError("Set a valid iot_code with deploy/iot-up.sh first")
    return code


def identity_settings(identity, code):
    if not CODE_PATTERN.fullmatch(code):
        raise RuntimeError("Invalid code; use 1-40 ASCII letters, digits, or hyphens")
    if identity not in ("gw", "consumer"):
        raise RuntimeError("Identity must be gw or consumer")
    return {
        "endpoint": (LOCAL_IOT / "endpoint").read_text(encoding="utf-8").strip(),
        "client_id": f"{identity}-{code}",
        "cert": LOCAL_IOT / f"{identity}.cert.pem",
        "key": LOCAL_IOT / f"{identity}.private.key",
        "ca": LOCAL_IOT / "AmazonRootCA1.pem",
        "topic": f"cxe/{code}/gw/events",
    }


def mqtt_client(client_id):
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise RuntimeError("Install paho-mqtt==2.1.0 before running W6 tools") from exc
    import ssl

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv5,
    )
    return mqtt, client, ssl


def reason_value(reason_code):
    return int(getattr(reason_code, "value", reason_code))


def configure_tls(client, settings, ssl):
    client.tls_set(
        ca_certs=str(settings["ca"]),
        certfile=str(settings["cert"]),
        keyfile=str(settings["key"]),
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )
