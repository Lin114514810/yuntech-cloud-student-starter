import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import consumer
from deploy import make_user_data
from deploy import iot_up
from tools import gateway_sim

ROOT = Path(__file__).resolve().parents[1]
reconcile_spec = importlib.util.spec_from_file_location(
    "w06_reconcile", ROOT / "tests" / "w06_reconcile.py"
)
w06_reconcile = importlib.util.module_from_spec(reconcile_spec)
reconcile_spec.loader.exec_module(w06_reconcile)


class GatewayBatchTests(unittest.TestCase):
    def test_batch_has_32_messages_with_one_exact_duplicate_and_one_unknown_device(self):
        prefix, events = gateway_sim.make_events("g03-m2", "r1")

        self.assertEqual(prefix, "g03-m2-r1")
        self.assertEqual(len(events), 32)
        self.assertEqual(events[0], events[30])
        self.assertEqual(events[31]["device_id"], "g03-m2-d99")
        self.assertEqual(events[31]["event_id"], "g03-m2-r1-d99-01")
        self.assertTrue(all(event["type"] == "status" for event in events))
        self.assertTrue(all(
            event["observed_at"].endswith("+00:00") for event in events
        ))

    def test_iot_policies_are_exact_and_separate_publish_from_subscribe(self):
        gateway = iot_up.policy_document("gw", "g03-m2", "123456789012", "us-east-1")
        consumer_policy = iot_up.policy_document(
            "consumer", "g03-m2", "123456789012", "us-east-1"
        )

        self.assertEqual(
            [statement["Action"] for statement in gateway["Statement"]],
            [["iot:Connect"], ["iot:Publish"]],
        )
        self.assertEqual(
            [statement["Action"] for statement in consumer_policy["Statement"]],
            [["iot:Connect"], ["iot:Subscribe"], ["iot:Receive"]],
        )
        self.assertIn(
            "arn:aws:iot:us-east-1:123456789012:topicfilter/cxe/g03-m2/gw/events",
            consumer_policy["Statement"][1]["Resource"],
        )
        self.assertNotIn("*", json.dumps((gateway, consumer_policy)))

    def test_remote_reconciliation_shell_is_syntactically_valid(self):
        result = subprocess.run(
            ["bash", "-n"],
            input=w06_reconcile.REMOTE_SCRIPT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ConsumerValidationTests(unittest.TestCase):
    settings = {
        "device_ids": {"g03-m2-d01", "g03-m2-d02"},
        "reporter_token": "synthetic-token",
    }

    def test_unlisted_device_is_rejected_without_calling_api(self):
        payload = {
            "event_id": "g03-m2-r1-d99-01",
            "device_id": "g03-m2-d99",
            "observed_at": "2026-10-13T01:00:00+00:00",
            "type": "status",
        }
        with patch.object(consumer, "post_event") as post_event:
            event_id, result = consumer.process_message(
                json.dumps(payload).encode(), self.settings
            )

        self.assertEqual((event_id, result), ("g03-m2-r1-d99-01", "rejected"))
        post_event.assert_not_called()

    def test_listed_device_is_forwarded_to_api(self):
        payload = {
            "event_id": "g03-m2-r1-d01-01",
            "device_id": "g03-m2-d01",
            "observed_at": "2026-10-13T01:00:00+00:00",
            "type": "status",
        }
        with patch.object(consumer, "post_event", return_value="created") as post_event:
            event_id, result = consumer.process_message(
                json.dumps(payload).encode(), self.settings
            )

        self.assertEqual((event_id, result), ("g03-m2-r1-d01-01", "created"))
        post_event.assert_called_once_with(payload, "synthetic-token")

    def test_invalid_payload_has_safe_rejection_log_id(self):
        event_id, result = consumer.process_message(b"not json", self.settings)
        self.assertEqual((event_id, result), ("invalid", "rejected"))

    def test_api_statuses_map_to_consumer_results(self):
        class Response:
            def __init__(self, status):
                self.status = status

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        payload = {"event_id": "event-1"}
        for status, expected in (
            (201, "created"),
            (200, "duplicate"),
            (400, "rejected"),
            (503, "error"),
        ):
            with self.subTest(status=status), patch.object(
                consumer.urllib.request,
                "urlopen",
                return_value=Response(status),
            ):
                self.assertEqual(
                    consumer.post_event(payload, "synthetic-token"), expected
                )

        with patch.object(
            consumer.urllib.request,
            "urlopen",
            side_effect=OSError("synthetic network failure"),
        ):
            self.assertEqual(
                consumer.post_event(payload, "synthetic-token"), "error"
            )


class DeploymentPackageTests(unittest.TestCase):
    def test_committed_consumer_is_included_with_pinned_runtime_and_service(self):
        commit = "a" * 40

        def fake_check_output(command, **kwargs):
            if command[1] == "rev-parse":
                return commit + "\n"
            name = command[2].split(":", 1)[1]
            return (ROOT / name).read_bytes()

        with patch.object(
            make_user_data.subprocess,
            "check_output",
            side_effect=fake_check_output,
        ), patch.object(
            make_user_data.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0),
        ):
            _, user_data = make_user_data.build("HEAD")

        text = user_data.decode()
        self.assertIn("paho-mqtt==2.1.0", text)
        self.assertIn("inspection-consumer.service", text)
        self.assertIn("systemctl enable inspection-consumer", text)
        self.assertLess(len(user_data), 16 * 1024)


if __name__ == "__main__":
    unittest.main()
