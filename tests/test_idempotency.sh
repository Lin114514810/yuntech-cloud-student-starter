#!/usr/bin/env bash
set -euo pipefail

HOST="${1:?請提供目標主機 IP: bash tests/test_idempotency.sh <HOST_IP>}"
BASE_URL="http://${HOST}"
SSH_KEY="${HOME}/.ssh/w03_key"
SSH_USER="ec2-user"

if [ ! -f .local/app.env ]; then
  echo "錯誤: 找不到 .local/app.env" >&2
  exit 1
fi
if [ "$(stat -c '%a' .local/app.env 2>/dev/null || true)" != "600" ]; then
  echo "錯誤: .local/app.env 權限必須為 600" >&2
  exit 1
fi
if [ ! -f "$SSH_KEY" ]; then
  echo "錯誤: 找不到 SSH 金鑰 $SSH_KEY" >&2
  exit 1
fi

# This file is a local, student-owned secret file; never print its values.
set -a
source .local/app.env
set +a
if [ -z "${REPORTER_TOKEN:-}" ] || [ -z "${OPERATOR_TOKEN:-}" ]; then
  echo "錯誤: app.env 缺少 REPORTER_TOKEN 或 OPERATOR_TOKEN" >&2
  exit 1
fi

HEALTH_RESULT=$(curl --silent --show-error --max-time 8 -w $'\n%{http_code}' "$BASE_URL/health")
HEALTH_CODE="${HEALTH_RESULT##*$'\n'}"
HEALTH_BODY="${HEALTH_RESULT%$'\n'*}"
if [ "$HEALTH_CODE" != "200" ]; then
  echo "health HTTP=$HEALTH_CODE; service health check failed" >&2
  exit 1
fi
HEALTH_FIELDS=$(printf '%s' "$HEALTH_BODY" | python3 -c '
import json, sys
health = json.load(sys.stdin)
print("{}\t{}".format(
    health.get("version", "missing"),
    str(health.get("db_configured", False)).lower()
))
')
IFS=$'\t' read -r HEALTH_VERSION DB_CONFIGURED <<< "$HEALTH_FIELDS"
printf 'health HTTP %s; version=%s; db_configured=%s\n' "$HEALTH_CODE" "$HEALTH_VERSION" "$DB_CONFIGURED"
if [ "$DB_CONFIGURED" != "true" ]; then
  echo "錯誤: 服務尚未載入資料庫秘密；請先確認部署狀態。" >&2
  exit 1
fi

EVENT_ID="w5-$(date -u +%Y%m%dT%H%M%S%N)-$$"
EVENT_DATA=$(printf '{"event_id":"%s","device_id":"w5-device","observed_at":"%s","type":"test","note":"w5-idempotency"}' \
  "$EVENT_ID" "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)")
CONFLICT_DATA="${EVENT_DATA/w5-idempotency/w5-different-note}"
FAILURES=0

request() {
  local response
  HTTP_BODY=""
  HTTP_CODE="000"
  response=$(curl --silent --show-error --max-time 10 -w $'\n%{http_code}' "$@") || {
    HTTP_BODY="request failed"
    HTTP_CODE="000"
    return 1
  }
  HTTP_CODE="${response##*$'\n'}"
  HTTP_BODY="${response%$'\n'*}"
}

show_row() {
  local number="$1" expected="$2"
  printf '%s. HTTP %s; body: %s\n' "$number" "$HTTP_CODE" "$HTTP_BODY"
  if [ "$HTTP_CODE" != "$expected" ]; then
    FAILURES=$((FAILURES + 1))
  fi
}

request -X POST "$BASE_URL/events" \
  -H "Authorization: Bearer ${REPORTER_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$EVENT_DATA" || true
show_row 1 201

request -X POST "$BASE_URL/events" \
  -H "Authorization: Bearer ${REPORTER_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$EVENT_DATA" || true
show_row 2 200

request -X POST "$BASE_URL/events" \
  -H "Authorization: Bearer ${REPORTER_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$CONFLICT_DATA" || true
show_row 3 409

if ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new \
  "${SSH_USER}@${HOST}" "sudo systemctl restart inspection"; then
  sleep 2
  request -H "Authorization: Bearer ${OPERATOR_TOKEN}" "$BASE_URL/events/${EVENT_ID}" || true
else
  HTTP_CODE="000"
  HTTP_BODY="service restart failed"
fi
show_row 4 200

COUNT=$(ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new \
  "${SSH_USER}@${HOST}" "sudo bash -s -- '${EVENT_ID}'" <<'REMOTE' 2>/dev/null || true
set -euo pipefail
event_id="$1"
set -a
. /etc/inspection/app.env
set +a
PGPASSWORD="$DB_PASSWORD" psql \
  "host=$DB_HOST dbname=$DB_NAME user=$DB_USER sslmode=verify-full sslrootcert=/etc/inspection/rds-ca.pem" \
  -v event_id="$event_id" -At <<'SQL'
SELECT count(*) FROM events WHERE event_id = :'event_id';
SQL
REMOTE
)
if [[ "$COUNT" =~ ^[0-9]+$ ]]; then
  if [ "$COUNT" != "1" ]; then
    FAILURES=$((FAILURES + 1))
  fi
  printf '5. EC2 psql count for the test event: %s\n' "$COUNT"
else
  printf '5. EC2 psql count for the test event: query failed\n'
  FAILURES=$((FAILURES + 1))
fi

if [ "$FAILURES" -gt 0 ]; then
  echo "矩陣未通過：${FAILURES} 項不符預期。" >&2
  exit 1
fi
echo "矩陣通過。"
