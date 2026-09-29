#!/usr/bin/env bash
set -euo pipefail

# 檢查本機秘密檔權限是否為 600
if [ "$(stat -c '%a' .local/app.env 2>/dev/null || true)" != "600" ]; then
  echo "錯誤: .local/app.env 權限必須為 600" >&2
  exit 1
fi

SSH_KEY="${HOME}/.ssh/w03_key"
if [ ! -f "$SSH_KEY" ]; then
  echo "錯誤: 找不到 SSH 金鑰 $SSH_KEY" >&2
  exit 1
fi

COMMIT_ID=$(git rev-parse HEAD)
TARGET_HOST="${1:?請提供目標主機公開 IP: ./deploy/deploy.sh <HOST_IP>}"
SSH_USER="ec2-user"

echo "==========================================="
echo "即將部署版本: ${COMMIT_ID}"
echo "目標伺服器:   ${TARGET_HOST}"
echo "==========================================="
read -r -p "確認部署請輸入 'yes': " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
  echo "部署已取消"
  exit 0
fi

# 1. 產生安裝腳本 (用 -u 避免預先建立檔案引發 FileExistsError)
USER_DATA_FILE=$(mktemp -u)
python3 deploy/make_user_data.py "$COMMIT_ID" "$USER_DATA_FILE"

# 2. 經 SSH 傳送並執行安裝腳本
echo "正在遠端執行安裝更新..."
ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "${SSH_USER}@${TARGET_HOST}" "sudo bash -s" < "$USER_DATA_FILE"
rm -f "$USER_DATA_FILE"

# 3. 經 SSH stdin 傳送 app.env 到 /etc/inspection/app.env 並重啟服務
echo "正在傳送設定檔並重啟服務..."
ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "${SSH_USER}@${TARGET_HOST}" "sudo mkdir -p /etc/inspection && sudo tee /etc/inspection/app.env > /dev/null && sudo chmod 600 /etc/inspection/app.env && sudo chown root:root /etc/inspection/app.env && sudo systemctl restart inspection" < .local/app.env

# 4. 健康檢查
echo "驗證服務狀態..."
sleep 2
HEALTH_RES=$(curl -s "http://${TARGET_HOST}/health")
echo "Health Response: ${HEALTH_RES}"

AUTH_OK=$(echo "$HEALTH_RES" | grep -o '"auth_configured":true' || true)
if [ -n "$AUTH_OK" ]; then
  echo "✅ 部署成功且 auth_configured 為 true！"
else
  echo "❌ 部署後驗證未通過，請檢查服務日誌。"
  exit 1
fi
