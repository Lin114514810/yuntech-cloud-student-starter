import json
from datetime import datetime, timezone
import os
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

from app import service


class FakeCursor:
    def __init__(self, insert_row, existing_row=None):
        self.insert_row = insert_row
        self.existing_row = existing_row
        self.statements = []
        self.last_result = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        if statement.lstrip().startswith("INSERT"):
            self.last_result = self.insert_row
        elif statement.lstrip().startswith("SELECT"):
            self.last_result = self.existing_row

    def fetchone(self):
        return self.last_result

    def fetchall(self):
        return []


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


class DatabaseIdempotencyTests(unittest.TestCase):
    data = {
        "event_id": "w5-event-1",
        "device_id": "device-1",
        "observed_at": "2026-10-06T01:00:00+00:00",
        "type": "test",
        "note": "same",
    }
    row = (
        "w5-event-1",
        "device-1",
        datetime(2026, 10, 6, 1, 0, tzinfo=timezone.utc),
        "test",
        "same",
        datetime(2026, 10, 6, 1, 1, tzinfo=timezone.utc),
    )

    def test_new_event_uses_parameterized_insert_before_any_lookup(self):
        cursor = FakeCursor(self.row)
        connection = FakeConnection(cursor)
        with patch.object(service, "connect_database", return_value=connection):
            outcome, record = service.insert_event(self.data)

        self.assertEqual(outcome, "created")
        self.assertEqual(record["event_id"], self.data["event_id"])
        statements = [statement for statement, _ in cursor.statements]
        self.assertTrue(statements[0].lstrip().startswith("CREATE TABLE"))
        self.assertIn("ON CONFLICT (event_id) DO NOTHING", statements[1])
        self.assertFalse(any(statement.lstrip().startswith("SELECT") for statement in statements[:2]))
        self.assertEqual(
            cursor.statements[1][1],
            ("w5-event-1", "device-1", "2026-10-06T01:00:00+00:00", "test", "same"),
        )
        self.assertTrue(connection.closed)

    def test_duplicate_same_payload_is_idempotent_and_changed_payload_conflicts(self):
        cursor = FakeCursor(None, self.row)
        with patch.object(service, "connect_database", return_value=FakeConnection(cursor)):
            outcome, _ = service.insert_event(self.data)
        self.assertEqual(outcome, "duplicate")

        changed = dict(self.data, note="changed")
        cursor = FakeCursor(None, self.row)
        with patch.object(service, "connect_database", return_value=FakeConnection(cursor)):
            outcome, _ = service.insert_event(changed)
        self.assertEqual(outcome, "conflict")

    def test_database_connection_requires_full_tls_verification(self):
        class DriverError(Exception):
            pass

        connection = object()
        driver = SimpleNamespace(connect=Mock(return_value=connection), Error=DriverError)
        with patch.dict(
            os.environ,
            {
                "DB_HOST": "db.example.invalid",
                "DB_NAME": "inspection",
                "DB_USER": "inspection_admin",
                "DB_PASSWORD": "synthetic-secret",
            },
        ), patch.dict(sys.modules, {"psycopg": driver}):
            result = service.connect_database()

        self.assertIs(result, connection)
        self.assertEqual(
            driver.connect.call_args.kwargs,
            {
                "host": "db.example.invalid",
                "dbname": "inspection",
                "user": "inspection_admin",
                "password": "synthetic-secret",
                "sslmode": "verify-full",
                "sslrootcert": service.RDS_CA_FILE,
                "connect_timeout": 5,
            },
        )

    def test_database_connection_error_records_safe_phase_and_sqlstate(self):
        class DriverError(Exception):
            sqlstate = "08001"

        driver = SimpleNamespace(
            connect=Mock(side_effect=DriverError("synthetic-host synthetic-password")),
            Error=DriverError,
        )
        with patch.dict(
            os.environ,
            {
                "DB_HOST": "db.example.invalid",
                "DB_NAME": "inspection",
                "DB_USER": "inspection_admin",
                "DB_PASSWORD": "synthetic-secret",
            },
        ), patch.dict(sys.modules, {"psycopg": driver}):
            with self.assertRaises(service.DatabaseUnavailable) as caught:
                service.connect_database()

        self.assertEqual(caught.exception.phase, "connect")
        self.assertEqual(caught.exception.reason, "DriverError")
        self.assertEqual(caught.exception.sqlstate, "08001")
        self.assertNotIn("synthetic", str(caught.exception))


class HttpContractTests(unittest.TestCase):
    def setUp(self):
        self.environ = patch.dict(
            "os.environ",
            {
                "REPORTER_TOKEN": "reporter-test",
                "OPERATOR_TOKEN": "operator-test",
            },
            clear=True,
        )
        self.environ.start()
        self.server = service.make_server(version_file="/does-not-exist", port=0)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

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

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.environ.stop()

    def test_health_reports_unconfigured_database_without_failing(self):
        with urllib.request.urlopen(self.base + "/health") as response:
            result = json.load(response)
        self.assertEqual(response.status, 200)
        self.assertFalse(result["db_configured"])

    def test_post_statuses_follow_database_outcome(self):
        event = dict(DatabaseIdempotencyTests.data)
        cases = (
            ("created", 201),
            ("duplicate", 200),
            ("conflict", 409),
        )
        for outcome, expected in cases:
            with self.subTest(outcome=outcome):
                with patch.object(service, "insert_event", return_value=(outcome, event)):
                    request = urllib.request.Request(
                        self.base + "/events",
                        data=json.dumps(event).encode("utf-8"),
                        headers={
                            "Authorization": "Bearer reporter-test",
                            "Content-Type": "application/json",
                        },
                        method="POST",
                    )
                    try:
                        response = urllib.request.urlopen(request)
                    except urllib.error.HTTPError as exc:
                        response = exc
                    self.assertEqual(response.status, expected)

    def test_database_failure_response_does_not_include_connection_details(self):
        event = dict(DatabaseIdempotencyTests.data)
        with patch.object(
            service,
            "insert_event",
            side_effect=service.DatabaseUnavailable("OperationalError"),
        ):
            status, response = self.call(
                "/events", "POST", event, "reporter-test"
            )
        self.assertEqual(status, 503)
        self.assertEqual(response, {"error": "Database unavailable"})
