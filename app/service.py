import json
import os
import re
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

# 記憶體存儲
events_store = {}

def get_tokens():
    return os.environ.get("REPORTER_TOKEN", ""), os.environ.get("OPERATOR_TOKEN", "")

def validate_iso8601_with_tz(date_str):
    try:
        dt = datetime.fromisoformat(date_str)
        return dt.tzinfo is not None
    except Exception:
        return False

INDEX_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Inspection Events</title></head>
<body>
  <h2>Inspection Events</h2>
  <input type="password" id="opToken" placeholder="貼上 OPERATOR 權杖">
  <button id="loadBtn">載入事件清單</button>
  <div id="eventsContainer"></div>

  <script>
    document.getElementById('loadBtn').addEventListener('click', () => {
      const token = document.getElementById('opToken').value;
      fetch('/events', {
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

class InspectionHandler(BaseHTTPRequestHandler):
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

    def do_GET(self):
        rep_token, op_token = get_tokens()

        if self.path == "/health":
            auth_configured = bool(rep_token and op_token)
            version = os.environ.get("APP_VERSION", "unknown")
            self.send_json(200, {
                "status": "ok",
                "service": "inspection",
                "version": version,
                "auth_configured": auth_configured
            })
            return

        if self.path == "/":
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/events":
            token = self.get_bearer_token()
            if not token or (token != rep_token and token != op_token):
                self.send_json(401, {"error": "Unauthorized"})
                return
            if token != op_token:
                self.send_json(403, {"error": "Forbidden"})
                return

            items = list(events_store.values())[-50:]
            self.send_json(200, items)
            return

        if self.path.startswith("/events/"):
            token = self.get_bearer_token()
            if not token or (token != rep_token and token != op_token):
                self.send_json(401, {"error": "Unauthorized"})
                return
            if token != op_token:
                self.send_json(403, {"error": "Forbidden"})
                return

            event_id = self.path[len("/events/"):]
            if event_id not in events_store:
                self.send_json(404, {"error": "Not Found"})
                return
            self.send_json(200, events_store[event_id])
            return

        self.send_json(404, {"error": "Not Found"})

    def do_POST(self):
        rep_token, op_token = get_tokens()

        if self.path == "/events":
            token = self.get_bearer_token()
            # 1. 認證 (401)
            if not token or (token != rep_token and token != op_token):
                self.send_json(401, {"error": "Unauthorized"})
                return
            # 2. 授權 (403)
            if token != rep_token:
                self.send_json(403, {"error": "Forbidden"})
                return

            # 3. 大小與 Content-Type (400)
            length = int(self.headers.get("Content-Length", 0))
            if length > 4096:
                self.send_json(400, {"error": "Payload too large", "field": "body"})
                return

            content_type = self.headers.get("Content-Type", "")
            if "application/json" not in content_type:
                self.send_json(400, {"error": "Content-Type must be application/json", "field": "Content-Type"})
                return

            raw_body = self.rfile.read(length)
            try:
                data = json.loads(raw_body.decode("utf-8"))
            except Exception:
                self.send_json(400, {"error": "Invalid JSON body", "field": "body"})
                return

            if not isinstance(data, dict):
                self.send_json(400, {"error": "Invalid JSON body", "field": "body"})
                return

            allowed_keys = {"event_id", "device_id", "observed_at", "type", "note"}
            if not set(data.keys()).issubset(allowed_keys):
                extra = list(set(data.keys()) - allowed_keys)[0]
                self.send_json(400, {"error": "Extra field not allowed", "field": extra})
                return

            for req_field in ["event_id", "device_id", "observed_at", "type"]:
                if req_field not in data:
                    self.send_json(400, {"error": "Missing required field", "field": req_field})
                    return

            if not (isinstance(data["event_id"], str) and 1 <= len(data["event_id"]) <= 64 and re.fullmatch(r"[A-Za-z0-9_-]+", data["event_id"])):
                self.send_json(400, {"error": "Invalid event_id", "field": "event_id"})
                return

            if not (isinstance(data["device_id"], str) and 1 <= len(data["device_id"]) <= 32 and re.fullmatch(r"[A-Za-z0-9_-]+", data["device_id"])):
                self.send_json(400, {"error": "Invalid device_id", "field": "device_id"})
                return

            if not (isinstance(data["observed_at"], str) and validate_iso8601_with_tz(data["observed_at"])):
                self.send_json(400, {"error": "Invalid observed_at must be ISO8601 with timezone", "field": "observed_at"})
                return

            if data["type"] not in ["status", "anomaly", "test"]:
                self.send_json(400, {"error": "Invalid type", "field": "type"})
                return

            if "note" in data:
                if not isinstance(data["note"], str) or len(data["note"]) > 200:
                    self.send_json(400, {"error": "Invalid note", "field": "note"})
                    return

            # 4. 重複 (409)
            if data["event_id"] in events_store:
                self.send_json(409, {"error": "Conflict: event_id already exists", "field": "event_id"})
                return

            # 5. 建立 (201)
            record = dict(data)
            record["received_at"] = datetime.now(timezone.utc).isoformat()
            events_store[data["event_id"]] = record
            self.send_json(201, record)
            return

        self.send_json(404, {"error": "Not Found"})

def run_server():
    server_address = ("127.0.0.1", 8080)
    httpd = HTTPServer(server_address, InspectionHandler)
    httpd.serve_forever()

if __name__ == "__main__":
    run_server()
