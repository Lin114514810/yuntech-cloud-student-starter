import os
import re
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# 讀取環境變數
REPORTER_TOKEN = os.environ.get("REPORTER_TOKEN", "")
OPERATOR_TOKEN = os.environ.get("OPERATOR_TOKEN", "")

# 記憶體存儲
events_store = {}

def get_tokens():
    rep = os.environ.get("REPORTER_TOKEN", REPORTER_TOKEN)
    op = os.environ.get("OPERATOR_TOKEN", OPERATOR_TOKEN)
    return rep, op

def get_bearer_token():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None

def validate_iso8601_with_tz(date_str):
    try:
        dt = datetime.fromisoformat(date_str)
        return dt.tzinfo is not None
    except Exception:
        return False

@app.route("/health", methods=["GET"])
def health():
    version = os.environ.get("APP_VERSION", "unknown")
    rep, op = get_tokens()
    auth_configured = bool(rep and op)
    return jsonify({
        "status": "ok",
        "service": "inspection",
        "version": version,
        "auth_configured": auth_configured
    }), 200

@app.route("/events", methods=["POST"])
def post_event():
    rep_token, op_token = get_tokens()
    token = get_bearer_token()

    # 1. 認證優先 (401)
    if not token or (token != rep_token and token != op_token):
        return jsonify({"error": "Unauthorized"}), 401
    # 2. 授權 (403)
    if token != rep_token:
        return jsonify({"error": "Forbidden"}), 403

    # 3. 請求大小與格式驗證 (400)
    if request.content_length and request.content_length > 4096:
        return jsonify({"error": "Payload too large", "field": "body"}), 400
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json", "field": "Content-Type"}), 400

    data = request.get_json(silent=True)
    if data is None or not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON body", "field": "body"}), 400

    allowed_keys = {"event_id", "device_id", "observed_at", "type", "note"}
    if not set(data.keys()).issubset(allowed_keys):
        extra = list(set(data.keys()) - allowed_keys)[0]
        return jsonify({"error": "Extra field not allowed", "field": extra}), 400

    for req_field in ["event_id", "device_id", "observed_at", "type"]:
        if req_field not in data:
            return jsonify({"error": "Missing required field", "field": req_field}), 400

    if not (isinstance(data["event_id"], str) and 1 <= len(data["event_id"]) <= 64 and re.fullmatch(r"[A-Za-z0-9_-]+", data["event_id"])):
        return jsonify({"error": "Invalid event_id", "field": "event_id"}), 400

    if not (isinstance(data["device_id"], str) and 1 <= len(data["device_id"]) <= 32 and re.fullmatch(r"[A-Za-z0-9_-]+", data["device_id"])):
        return jsonify({"error": "Invalid device_id", "field": "device_id"}), 400

    if not (isinstance(data["observed_at"], str) and validate_iso8601_with_tz(data["observed_at"])):
        return jsonify({"error": "Invalid observed_at must be ISO8601 with timezone", "field": "observed_at"}), 400

    if data["type"] not in ["status", "anomaly", "test"]:
        return jsonify({"error": "Invalid type", "field": "type"}), 400

    if "note" in data:
        if not isinstance(data["note"], str) or len(data["note"]) > 200:
            return jsonify({"error": "Invalid note", "field": "note"}), 400

    # 4. 重複檢查 (409)
    if data["event_id"] in events_store:
        return jsonify({"error": "Conflict: event_id already exists", "field": "event_id"}), 409

    # 5. 建立 (201)
    record = dict(data)
    record["received_at"] = datetime.now(timezone.utc).isoformat()
    events_store[data["event_id"]] = record

    return jsonify(record), 201

@app.route("/events", methods=["GET"])
def get_events():
    rep_token, op_token = get_tokens()
    token = get_bearer_token()

    if not token or (token != rep_token and token != op_token):
        return jsonify({"error": "Unauthorized"}), 401
    if token != op_token:
        return jsonify({"error": "Forbidden"}), 403

    items = list(events_store.values())[-50:]
    return jsonify(items), 200

@app.route("/events/<event_id>", methods=["GET"])
def get_event_by_id(event_id):
    rep_token, op_token = get_tokens()
    token = get_bearer_token()

    if not token or (token != rep_token and token != op_token):
        return jsonify({"error": "Unauthorized"}), 401
    if token != op_token:
        return jsonify({"error": "Forbidden"}), 403

    if event_id not in events_store:
        return jsonify({"error": "Not Found"}), 404
    return jsonify(events_store[event_id]), 200

INDEX_HTML = """
<!DOCTYPE html>
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
</html>
"""

@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML), 200

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080)
