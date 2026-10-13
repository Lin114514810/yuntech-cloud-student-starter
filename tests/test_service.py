import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from app import service

ROOT = Path(__file__).resolve().parents[1]


class ServiceContractTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            "os.environ",
            {
                "REPORTER_TOKEN": "test-reporter",
                "OPERATOR_TOKEN": "test-operator",
            },
            clear=True,
        )
        self.env.start()
        self.server = service.make_server(version_file="/not-present", port=0)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.env.stop()

    def call(self, path, method="GET", body=None, token=None):
        headers = {}
        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            self.base + path, data=encoded, headers=headers, method=method
        )
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as exc:
            response = exc
        return response.status, json.loads(response.read().decode("utf-8"))

    def test_unauthorized_request_is_rejected_before_payload_validation(self):
        status, _ = self.call("/events", "POST", {"bad": "payload"})
        self.assertEqual(status, 401)

    def test_home_page_remains_available(self):
        request = urllib.request.Request(self.base + "/")
        with urllib.request.urlopen(request) as response:
            body = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("Inspection Events", body)

    def test_event_validation_and_idempotent_duplicate(self):
        valid_data = json.loads((ROOT / "tests/fixtures/valid_event.json").read_text())
        with patch.object(
            service,
            "insert_event",
            side_effect=[
                ("created", dict(valid_data, received_at="2026-10-06T01:00:00+00:00")),
                ("duplicate", dict(valid_data, received_at="2026-10-06T01:00:00+00:00")),
            ],
        ):
            status, response = self.call(
                "/events", "POST", valid_data, "test-reporter"
            )
            self.assertEqual(status, 201)
            self.assertIn("received_at", response)

            status, _ = self.call("/events", "POST", valid_data, "test-reporter")
            self.assertEqual(status, 200)

        invalid_tz = json.loads((ROOT / "tests/fixtures/invalid_no_tz.json").read_text())
        status, response = self.call("/events", "POST", invalid_tz, "test-reporter")
        self.assertEqual(status, 400)
        self.assertEqual(response["field"], "observed_at")

        extra_field = json.loads((ROOT / "tests/fixtures/invalid_extra_field.json").read_text())
        status, _ = self.call("/events", "POST", extra_field, "test-reporter")
        self.assertEqual(status, 400)

    def test_operator_read_permissions(self):
        status, _ = self.call("/events", token="test-reporter")
        self.assertEqual(status, 403)

        with patch.object(service, "list_events", return_value=[{"event_id": "one"}]):
            status, items = self.call("/events", token="test-operator")
        self.assertEqual(status, 200)
        self.assertEqual(items, [{"event_id": "one"}])

    def test_operator_can_filter_events_by_device_id(self):
        with patch.object(
            service, "list_events", return_value=[{"event_id": "one"}]
        ) as list_events:
            status, items = self.call(
                "/events?device_id=g03-m2-d01", token="test-operator"
            )
        self.assertEqual(status, 200)
        self.assertEqual(items, [{"event_id": "one"}])
        list_events.assert_called_once_with("g03-m2-d01")

    def test_invalid_device_filter_is_rejected(self):
        status, response = self.call(
            "/events?device_id=invalid%20device", token="test-operator"
        )
        self.assertEqual(status, 400)
        self.assertEqual(response["field"], "device_id")
