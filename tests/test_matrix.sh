#!/usr/bin/env bash
set -euo pipefail

HOST="${1:?請提供目標主機 IP: ./tests/test_matrix.sh <HOST_IP>}"
BASE_URL="http://${HOST}"

if [ ! -f .local/app.env ]; then
  echo "錯誤: 找不到 .local/app.env" >&2
  exit 1
fi
source .local/app.env

EVENT_DATA='{"event_id":"g03-m2-0001","device_id":"g03-d01","observed_at":"2026-09-29T10:00:00+08:00","type":"status","note":"check"}'
INVALID_TZ='{"event_id":"g03-m2-0002","device_id":"g03-d01","observed_at":"2026-09-29T10:00:00","type":"status"}'

echo "=== 執行 W4 拒絕矩陣 7 項檢查 ==="

echo -n "1. reporter 送一筆合法事件 (預期 201): "
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$BASE_URL/events" \
  -H "Authorization: Bearer $REPORTER_TOKEN" -H "Content-Type: application/json" -d "$EVENT_DATA"

echo -n "2. 同上，不帶權杖 (預期 401): "
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$BASE_URL/events" \
  -H "Content-Type: application/json" -d "$EVENT_DATA"

echo -n "3. operator 權杖送事件 (預期 403): "
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$BASE_URL/events" \
  -H "Authorization: Bearer $OPERATOR_TOKEN" -H "Content-Type: application/json" -d "$EVENT_DATA"

echo -n "4. reporter，observed_at 沒有時區 (預期 400): "
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$BASE_URL/events" \
  -H "Authorization: Bearer $REPORTER_TOKEN" -H "Content-Type: application/json" -d "$INVALID_TZ"

echo -n "5. reporter，再送一次 #1 (預期 409): "
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$BASE_URL/events" \
  -H "Authorization: Bearer $REPORTER_TOKEN" -H "Content-Type: application/json" -d "$EVENT_DATA"

echo -n "6. reporter 權杖讀清單 (預期 403): "
curl -s -o /dev/null -w "%{http_code}\n" -X GET "$BASE_URL/events" \
  -H "Authorization: Bearer $REPORTER_TOKEN"

echo -n "7. operator 權杖讀清單 (預期 200): "
curl -s -o /dev/null -w "%{http_code}\n" -X GET "$BASE_URL/events" \
  -H "Authorization: Bearer $OPERATOR_TOKEN"
