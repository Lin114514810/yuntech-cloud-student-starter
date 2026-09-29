#!/usr/bin/env bash
set -euo pipefail

STOP_ONLY=false
if [[ "${1:-}" == "--stop" ]]; then
  STOP_ONLY=true
fi

if [ -f .local/config.env ]; then
  source .local/config.env
else
  echo "錯誤: 找不到 .local/config.env"
  exit 1
fi

if [ ! -f .local/resources.json ]; then
  echo "錯誤: 找不到 .local/resources.json"
  exit 1
fi

INSTANCE_ID=$(grep -o '"instance_id": "[^"]*' .local/resources.json | cut -d'"' -f4 || true)
SG_ID=$(grep -o '"security_group_id": "[^"]*' .local/resources.json | cut -d'"' -f4 || true)
KEY_NAME=$(grep -o '"key_name": "[^"]*' .local/resources.json | cut -d'"' -f4 || true)

echo "=========================================="
if [ "$STOP_ONLY" = true ]; then
  echo "即將停止主機 (保留 SG 與金鑰對):"
  echo "  - 實例 ID: $INSTANCE_ID"
else
  echo "即將刪除/回收以下資源:"
  echo "  - 實例 ID: $INSTANCE_ID"
  echo "  - SG ID:   $SG_ID"
  echo "  - 金鑰對:  $KEY_NAME"
fi
echo "=========================================="
read -p "請確認是否執行？(y/N): " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
  echo "已取消。"
  exit 0
fi

if [ -n "$INSTANCE_ID" ]; then
  if [ "$STOP_ONLY" = true ]; then
    echo "--> 停止實例 $INSTANCE_ID..."
    aws ec2 stop-instances --instance-ids "$INSTANCE_ID" --profile "$PROFILE" >/dev/null
    aws ec2 wait instance-stopped --instance-ids "$INSTANCE_ID" --profile "$PROFILE"
    STATE=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" --query "Reservations[0].Instances[0].State.Name" --output text --profile "$PROFILE")
    echo "實例當前狀態: $STATE (驗證已停止)"
    exit 0
  else
    echo "--> 終止實例 $INSTANCE_ID..."
    aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" --profile "$PROFILE" >/dev/null
    echo "--> 等待實例終止完成..."
    aws ec2 wait instance-terminated --instance-ids "$INSTANCE_ID" --profile "$PROFILE"
    echo "實例已終止。"
  fi
fi

if [ -n "$SG_ID" ]; then
  echo "--> 刪除 Security Group $SG_ID..."
  # 嘗試刪除 SG (實例完全終止並釋放網路介面需要數秒)
  for i in {1..12}; do
    if aws ec2 delete-security-group --group-id "$SG_ID" --profile "$PROFILE" >/dev/null 2>&1; then
      echo "SG $SG_ID 已刪除。"
      break
    fi
    sleep 5
  done
fi

if [ -n "$KEY_NAME" ]; then
  echo "--> 刪除 AWS 金鑰對 $KEY_NAME..."
  aws ec2 delete-key-pair --key-name "$KEY_NAME" --profile "$PROFILE"
  echo "金鑰對已刪除。"
fi

# 讀回確認
echo "--> 驗證資源是否已全數清除..."
aws ec2 describe-security-groups --group-ids "$SG_ID" --profile "$PROFILE" 2>&1 | grep -q "InvalidGroup.NotFound" && echo "SG 確認已不存在。"
aws ec2 describe-key-pairs --key-names "$KEY_NAME" --profile "$PROFILE" 2>&1 | grep -q "InvalidKeyPair.NotFound" && echo "金鑰對確認已不存在。"

rm -f .local/resources.json
echo "資源回收與讀回確認完成！"
