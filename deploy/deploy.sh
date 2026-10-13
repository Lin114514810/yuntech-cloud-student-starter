#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

for file in .local/app.env .local/db.env; do
  if [ "$(stat -c '%a' "$file" 2>/dev/null || true)" != "600" ]; then
    echo "錯誤: $file 權限必須為 600" >&2
    exit 1
  fi
done
if ! grep -q '^DB_HOST=.' .local/db.env; then
  echo "錯誤: .local/db.env 尚未包含資料庫位址；請先完成 deploy/db-up.sh" >&2
  exit 1
fi

SSH_KEY="${HOME}/.ssh/w03_key"
if [ ! -f "$SSH_KEY" ]; then
  echo "錯誤: 找不到 SSH 金鑰 $SSH_KEY" >&2
  exit 1
fi
if [ "$(stat -c '%a' .local/iot 2>/dev/null || true)" != "700" ]; then
  echo "錯誤: .local/iot 權限必須為 700；請先完成 deploy/iot-up.sh" >&2
  exit 1
fi
for file in endpoint AmazonRootCA1.pem consumer.cert.pem consumer.private.key; do
  path=".local/iot/$file"
  if [ ! -f "$path" ] || [ "$(stat -c '%a' "$path" 2>/dev/null || true)" != "600" ]; then
    echo "錯誤: $path 必須存在且權限為 600" >&2
    exit 1
  fi
done

IOT_CODE=$(python3 - <<'PY'
import json
import re
with open(".local/resources.json", encoding="utf-8") as stream:
    code = json.load(stream).get("iot_code", "")
if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,40}", code):
    raise SystemExit("錯誤: .local/resources.json 缺少有效的 iot_code")
print(code)
PY
)
IOT_ENDPOINT=$(tr -d '\r\n' < .local/iot/endpoint)
if [[ ! "$IOT_ENDPOINT" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "錯誤: .local/iot/endpoint 格式無效" >&2
  exit 1
fi

COMMIT_ID=$(git rev-parse HEAD)
if ! git cat-file -e "${COMMIT_ID}:app/consumer.py" 2>/dev/null; then
  echo "錯誤: 請先 commit app/consumer.py，再部署 W6。" >&2
  exit 1
fi
TARGET_HOST="${1:?請提供目標主機公開 IP: ./deploy/deploy.sh <HOST_IP>}"
SSH_USER="ec2-user"
DEVICE_IDS=""
for number in $(seq -w 1 10); do
  DEVICE_IDS="${DEVICE_IDS:+$DEVICE_IDS,}${IOT_CODE}-d${number}"
done

echo "==========================================="
echo "即將部署版本: ${COMMIT_ID}"
echo "目標伺服器:   ${TARGET_HOST}"
echo "IoT 主題:     cxe/${IOT_CODE}/gw/events"
echo "遠端變更:     更新 /etc/inspection/app.env 與 consumer 的三個 TLS 檔案"
echo "服務變更:     重啟 inspection、inspection-consumer"
echo "費用／網路:   不建立 AWS 資源、不改安全群組；主機使用既有 outbound 8883"
echo "回復方式:     以既有部署流程回復前一個 commit 與設定，再重啟兩個服務"
echo "==========================================="
read -r -p "確認部署請輸入 'yes': " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
  echo "部署已取消"
  exit 0
fi

USER_DATA_DIR=$(mktemp -d)
USER_DATA_FILE="$USER_DATA_DIR/user-data.sh"
DEPLOY_ENV="$USER_DATA_DIR/app.env"
trap 'rm -f "$USER_DATA_FILE" "$DEPLOY_ENV"; rmdir "$USER_DATA_DIR"' EXIT
python3 deploy/make_user_data.py "$COMMIT_ID" "$USER_DATA_FILE"

python3 - "$TARGET_HOST" <<'PY'
import ipaddress
import json
import os
import sys
import tempfile
from pathlib import Path

path = Path(".local/resources.json")
try:
    host = str(ipaddress.IPv4Address(sys.argv[1]))
    resources = json.loads(path.read_text(encoding="utf-8"))
except (OSError, ValueError, json.JSONDecodeError) as exc:
    raise SystemExit("錯誤: 主機 IPv4 或 .local/resources.json 無效") from exc
if not isinstance(resources, dict):
    raise SystemExit("錯誤: .local/resources.json 必須是 JSON object")
resources["public_ip"] = host
fd, temp_name = tempfile.mkstemp(prefix=".resources-", dir=path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(resources, stream, indent=2)
        stream.write("\n")
    os.replace(temp_name, path)
finally:
    if os.path.exists(temp_name):
        os.unlink(temp_name)
PY

echo "正在遠端安裝應用程式..."
ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new \
  "${SSH_USER}@${TARGET_HOST}" "sudo bash -s" < "$USER_DATA_FILE"

echo "正在建立 consumer TLS 目錄並傳送憑證..."
ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new \
  "${SSH_USER}@${TARGET_HOST}" \
  "sudo install -d -o root -g inspection -m 755 /etc/inspection && sudo chmod 755 /etc/inspection && sudo install -d -o inspection -g inspection -m 700 /etc/inspection/iot"

install_iot_file() {
  local source_file="$1"
  local remote_name="$2"
  ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new \
    "${SSH_USER}@${TARGET_HOST}" \
    "sudo bash -c 'set -e; umask 077; cat > /etc/inspection/iot/${remote_name}.tmp; chown inspection:inspection /etc/inspection/iot/${remote_name}.tmp; chmod 600 /etc/inspection/iot/${remote_name}.tmp; mv /etc/inspection/iot/${remote_name}.tmp /etc/inspection/iot/${remote_name}'" \
    < "$source_file"
}
install_iot_file .local/iot/AmazonRootCA1.pem AmazonRootCA1.pem
install_iot_file .local/iot/consumer.cert.pem consumer.cert.pem
install_iot_file .local/iot/consumer.private.key consumer.private.key

umask 077
cat .local/app.env .local/db.env > "$DEPLOY_ENV"
printf 'IOT_ENDPOINT=%s\nIOT_TOPIC=cxe/%s/gw/events\nIOT_CLIENT_ID=consumer-%s\nIOT_DEVICE_IDS=%s\nIOT_CA=/etc/inspection/iot/AmazonRootCA1.pem\nIOT_CERT=/etc/inspection/iot/consumer.cert.pem\nIOT_KEY=/etc/inspection/iot/consumer.private.key\n' \
  "$IOT_ENDPOINT" "$IOT_CODE" "$IOT_CODE" "$DEVICE_IDS" >> "$DEPLOY_ENV"
chmod 600 "$DEPLOY_ENV"

echo "正在安裝設定並重啟服務..."
ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new \
  "${SSH_USER}@${TARGET_HOST}" \
  "sudo bash -c 'set -e; umask 077; cat > /etc/inspection/app.env.tmp; chown root:root /etc/inspection/app.env.tmp; chmod 600 /etc/inspection/app.env.tmp; mv /etc/inspection/app.env.tmp /etc/inspection/app.env; systemctl daemon-reload; systemctl restart inspection inspection-consumer; systemctl is-active inspection inspection-consumer'" \
  < "$DEPLOY_ENV"

echo "驗證服務健康狀態..."
sleep 2
HEALTH_RES=$(curl -fsS --max-time 8 "http://${TARGET_HOST}/health")
echo "Health Response: ${HEALTH_RES}"
if ! echo "$HEALTH_RES" | grep -q "\"version\":\"${COMMIT_ID}\""; then
  echo "錯誤: 部署版本與目前 commit 不符" >&2
  exit 1
fi
if ! echo "$HEALTH_RES" | grep -q '"auth_configured":[ ]*true' \
  || ! echo "$HEALTH_RES" | grep -q '"db_configured":[ ]*true'; then
  echo "錯誤: auth_configured 或 db_configured 未通過健康檢查" >&2
  exit 1
fi
echo "部署成功；inspection 與 inspection-consumer 均已重新啟動。"
