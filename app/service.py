import json
import os
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

RDS_CA_FILE = "/etc/inspection/rds-ca.pem"
_STARTED_AT = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
INDEX_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Inspection Events</title></head>
<body>
  <h2>Inspection Events</h2>
  <input type="password" id="opToken" placeholder="貼上 OPERATOR 權杖">
  <input type="text" id="deviceId" placeholder="設備編號">
  <button id="loadBtn">載入事件清單</button>
  <div id="eventsContainer"></div>

  <script>
    document.getElementById('loadBtn').addEventListener('click', () => {
      const token = document.getElementById('opToken').value;
      const deviceId = document.getElementById('deviceId').value.trim();
      const query = deviceId ? '?device_id=' + encodeURIComponent(deviceId) : '';
      fetch('/events' + query, {
        headers: { 'Authorization': 'Bearer ' + token }
      })
      .then(res => res.json())
      .then(data => {
        const container = document.getElementById('eventsContainer');
        container.replaceChildren();
        if (Array.isArray(data)) {
          data.forEach(item => {
            const pre = document.createElement('pre');
            pre.textContent = JSON.stringify(item, null, 2);
            container.appendChild(pre);
          });
        } else {
          const pre = document.createElement('pre');
          pre.textContent = JSON.stringify(data, null, 2);
          container.appendChild(pre);
        }
      });
    });
  </script>
</body>
</html>"""


class DatabaseUnavailable(Exception):
    def __init__(self, reason, phase="unknown", sqlstate=None):
        self.reason = reason
        self.phase = phase
        self.sqlstate = sqlstate
        super().__init__(reason)


def get_tokens():
    return os.environ.get("REPORTER_TOKEN", ""), os.environ.get("OPERATOR_TOKEN", "")


def validate_iso8601_with_tz(date_str):
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.tzinfo is not None
    except (TypeError, ValueError):
        return False


def database_configured():
    return all(os.environ.get(key) for key in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"))


def connect_database():
    if not database_configured():
        raise DatabaseUnavailable("not_configured", phase="configuration")
    try:
        import psycopg
    except ImportError as exc:
        raise DatabaseUnavailable("driver_unavailable", phase="driver_import") from exc

    try:
        return psycopg.connect(
            host=os.environ["DB_HOST"],
            dbname=os.environ["DB_NAME"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            sslmode="verify-full",
            sslrootcert=RDS_CA_FILE,
            connect_timeout=5,
        )
    except psycopg.Error as exc:
        raise DatabaseUnavailable(
            type(exc).__name__,
            phase="connect",
            sqlstate=exc.sqlstate,
        ) from exc


def ensure_events_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            type TEXT NOT NULL,
            note TEXT,
            received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _record(row):
    if row is None:
        return None
    observed_at = row[2]
    received_at = row[5]
    result = {
        "event_id": row[0],
        "device_id": row[1],
        "observed_at": observed_at.isoformat() if hasattr(observed_at, "isoformat") else observed_at,
        "type": row[3],
        "received_at": received_at.isoformat() if hasattr(received_at, "isoformat") else received_at,
    }
    if row[4] is not None:
        result["note"] = row[4]
    return result


def _normalized_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def insert_event(data):
    connection = connect_database()
    try:
        with connection:
            with connection.cursor() as cursor:
                ensure_events_table(cursor)
                cursor.execute(
                    """
                    INSERT INTO events (event_id, device_id, observed_at, type, note)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (event_id) DO NOTHING
                    RETURNING event_id, device_id, observed_at, type, note, received_at
                    """,
                    (
                        data["event_id"],
                        data["device_id"],
                        data["observed_at"],
                        data["type"],
                        data.get("note"),
                    ),
                )
                inserted = cursor.fetchone()
                if inserted is not None:
                    return "created", _record(inserted)

                cursor.execute(
                    """
                    SELECT event_id, device_id, observed_at, type, note, received_at
                    FROM events WHERE event_id = %s
                    """,
                    (data["event_id"],),
                )
                existing = cursor.fetchone()
                if existing is None:
                    raise DatabaseUnavailable("conflict_row_missing", phase="query")

                same_content = (
                    existing[1] == data["device_id"]
                    and _normalized_time(data["observed_at"]) == existing[2].astimezone(timezone.utc)
                    and existing[3] == data["type"]
                    and existing[4] == data.get("note")
                )
                return ("duplicate" if same_content else "conflict"), _record(existing)
    except DatabaseUnavailable:
        raise
    except Exception as exc:
        raise DatabaseUnavailable(
            type(exc).__name__,
            phase="query",
            sqlstate=getattr(exc, "sqlstate", None),
        ) from exc
    finally:
        connection.close()


def list_events(device_id=None):
    connection = connect_database()
    try:
        with connection:
            with connection.cursor() as cursor:
                ensure_events_table(cursor)
                if device_id:
                    cursor.execute(
                        """
                        SELECT event_id, device_id, observed_at, type, note, received_at
                        FROM events WHERE device_id = %s
                        ORDER BY received_at DESC LIMIT 50
                        """,
                        (device_id,),
                    )
                    return [_record(row) for row in cursor.fetchall()]

                cursor.execute(
                    """
                    SELECT event_id, device_id, observed_at, type, note, received_at
                    FROM events ORDER BY received_at DESC LIMIT 50
                    """
                )
                return [_record(row) for row in cursor.fetchall()][::-1]
    except DatabaseUnavailable:
        raise
    except Exception as exc:
        raise DatabaseUnavailable(
            type(exc).__name__,
            phase="query",
            sqlstate=getattr(exc, "sqlstate", None),
        ) from exc
    finally:
        connection.close()


def get_event(event_id):
    connection = connect_database()
    try:
        with connection:
            with connection.cursor() as cursor:
                ensure_events_table(cursor)
                cursor.execute(
                    """
                    SELECT event_id, device_id, observed_at, type, note, received_at
                    FROM events WHERE event_id = %s
                    """,
                    (event_id,),
                )
                return _record(cursor.fetchone())
    except DatabaseUnavailable:
        raise
    except Exception as exc:
        raise DatabaseUnavailable(
            type(exc).__name__,
            phase="query",
            sqlstate=getattr(exc, "sqlstate", None),
        ) from exc
    finally:
        connection.close()


def load_version(version_file):
    version_path = Path(version_file)
    if version_path.exists():
        version = version_path.read_text(encoding="utf-8").strip()
    else:
        version = os.environ.get("APP_VERSION", "unknown")
    if version != "unknown" and not re.fullmatch(r"[0-9a-f]{40}", version):
        raise ValueError("Invalid deployment version")
    return version


class InspectionHandler(BaseHTTPRequestHandler):
    server_version = "Inspection/1"

    def send_json(self, status_code, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def get_bearer_token(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return None

    def require_operator(self, reporter_token, operator_token):
        token = self.get_bearer_token()
        if not token or (token != reporter_token and token != operator_token):
            self.send_json(401, {"error": "Unauthorized"})
            return False
        if token != operator_token:
            self.send_json(403, {"error": "Forbidden"})
            return False
        return True

    def report_database_error(self, exc):
        self.log_error(
            "database unavailable phase=%s type=%s sqlstate=%s",
            exc.phase,
            exc.reason,
            exc.sqlstate or "none",
        )
        self.send_json(503, {"error": "Database unavailable"})

    def do_GET(self):
        reporter_token, operator_token = get_tokens()
        request_path = urlsplit(self.path)

        if request_path.path == "/health" and not request_path.query:
            self.send_json(
                200,
                {
                    "status": "ok",
                    "service": "inspection",
                    "version": self.server.version,
                    "started_at": self.server.started_at,
                    "auth_configured": bool(reporter_token and operator_token),
                    "db_configured": database_configured(),
                },
            )
            return

        if request_path.path == "/" and not request_path.query:
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if request_path.path == "/events":
            if not self.require_operator(reporter_token, operator_token):
                return
            query = parse_qs(request_path.query, keep_blank_values=True)
            device_ids = query.get("device_id", [])
            if len(device_ids) > 1 or (device_ids and not device_ids[0]):
                self.send_json(400, {"error": "Invalid device_id", "field": "device_id"})
                return
            device_id = device_ids[0] if device_ids else None
            if device_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", device_id):
                self.send_json(400, {"error": "Invalid device_id", "field": "device_id"})
                return
            try:
                events = list_events(device_id) if device_id is not None else list_events()
                self.send_json(200, events)
            except DatabaseUnavailable as exc:
                self.report_database_error(exc)
            return

        if request_path.path.startswith("/events/"):
            if not self.require_operator(reporter_token, operator_token):
                return
            event_id = unquote(request_path.path[len("/events/"):])
            try:
                result = get_event(event_id)
            except DatabaseUnavailable as exc:
                self.report_database_error(exc)
                return
            if result is None:
                self.send_json(404, {"error": "Not Found"})
            else:
                self.send_json(200, result)
            return

        self.send_json(404, {"error": "Not Found"})

    def do_POST(self):
        reporter_token, operator_token = get_tokens()

        if self.path != "/events":
            self.send_json(404, {"error": "Not Found"})
            return

        token = self.get_bearer_token()
        if not token or (token != reporter_token and token != operator_token):
            self.send_json(401, {"error": "Unauthorized"})
            return
        if token != reporter_token:
            self.send_json(403, {"error": "Forbidden"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.send_json(400, {"error": "Invalid Content-Length", "field": "body"})
            return
        if length < 0 or length > 4096:
            self.send_json(400, {"error": "Payload too large", "field": "body"})
            return

        if "application/json" not in self.headers.get("Content-Type", ""):
            self.send_json(400, {"error": "Content-Type must be application/json", "field": "Content-Type"})
            return

        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, {"error": "Invalid JSON body", "field": "body"})
            return
        if not isinstance(data, dict):
            self.send_json(400, {"error": "Invalid JSON body", "field": "body"})
            return

        allowed_keys = {"event_id", "device_id", "observed_at", "type", "note"}
        extra_keys = set(data) - allowed_keys
        if extra_keys:
            self.send_json(400, {"error": "Extra field not allowed", "field": sorted(extra_keys)[0]})
            return
        for required in ("event_id", "device_id", "observed_at", "type"):
            if required not in data:
                self.send_json(400, {"error": "Missing required field", "field": required})
                return

        if not isinstance(data["event_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", data["event_id"]):
            self.send_json(400, {"error": "Invalid event_id", "field": "event_id"})
            return
        if not isinstance(data["device_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", data["device_id"]):
            self.send_json(400, {"error": "Invalid device_id", "field": "device_id"})
            return
        if not isinstance(data["observed_at"], str) or not validate_iso8601_with_tz(data["observed_at"]):
            self.send_json(400, {"error": "Invalid observed_at must be ISO8601 with timezone", "field": "observed_at"})
            return
        if data["type"] not in ("status", "anomaly", "test"):
            self.send_json(400, {"error": "Invalid type", "field": "type"})
            return
        if "note" in data and (not isinstance(data["note"], str) or len(data["note"]) > 200):
            self.send_json(400, {"error": "Invalid note", "field": "note"})
            return

        try:
            outcome, record = insert_event(data)
        except DatabaseUnavailable as exc:
            self.report_database_error(exc)
            return

        if outcome == "created":
            self.send_json(201, record)
        elif outcome == "duplicate":
            self.send_json(200, record)
        else:
            self.send_json(409, {"error": "Conflict: event_id already exists", "field": "event_id"})

    def log_message(self, format, *args):
        super().log_message(format, *args)


def make_server(version_file="/opt/inspection/app/version", host="127.0.0.1", port=8080):
    server = HTTPServer((host, port), InspectionHandler)
    server.version = load_version(version_file)
    server.started_at = _STARTED_AT
    return server


def run_server():
    server = make_server()
    server.serve_forever()


if __name__ == "__main__":
    run_server()
