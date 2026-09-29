#!/usr/bin/env bash
set -euo pipefail

# 載入本機設定
if [ -f .local/config.env ]; then
  source .local/config.env
else
  echo "錯誤: 找不到 .local/config.env"
  exit 1
fi

COMMIT_SHA=$(git rev-parse HEAD)
USER_DATA_FILE=".local/w03-user-data.sh"

echo "=========================================="
echo "即將執行的建立清單 (Plan):"
echo "  - AWS Profile: $PROFILE"
echo "  - 預設 VPC ID: $VPC_ID"
echo "  - 預設 Subnet: $SUBNET_ID"
echo "  - AMI ID:      $AMI_ID"
echo "  - 來源 CIDR:   $MY_IP_CIDR (TCP 22, 80)"
echo "  - 金鑰對名稱:  $KEY_NAME"
echo "  - SG 名稱:     $SG_NAME"
echo "  - 實例規格:    t3.micro, gp3 8GB Encrypted, IMDSv2 Required"
echo "  - 部署版本:    $COMMIT_SHA"
echo "  - 標籤:        course=yuntech-115-1, week=w03, group=$GROUP, owner=$OWNER"
echo "=========================================="
read -p "請確認是否執行建立？(y/N): " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
  echo "已取消建立。"
  exit 0
fi

mkdir -p .local
echo "{}" > .local/resources.json

# 1. 產生 user data
echo "--> 打包 User Data..."
rm -f "$USER_DATA_FILE"
bash deploy/make-user-data.sh HEAD "$USER_DATA_FILE"

# 2. 匯入金鑰對
echo "--> 檢查並匯入金鑰對..."
if ! aws ec2 describe-key-pairs --key-names "$KEY_NAME" --profile "$PROFILE" >/dev/null 2>&1; then
  aws ec2 import-key-pair \
    --key-name "$KEY_NAME" \
    --public-key-material "fileb://$HOME/.ssh/w03_key.pub" \
    --tag-specifications "ResourceType=key-pair,Tags=[{Key=course,Value=yuntech-115-1},{Key=week,Value=w03},{Key=group,Value=$GROUP},{Key=owner,Value=$OWNER}]" \
    --profile "$PROFILE" >/dev/null
  echo "金鑰對 $KEY_NAME 已匯入。"
else
  echo "金鑰對 $KEY_NAME 已存在。"
fi

# 3. 建立 Security Group
echo "--> 建立 Security Group..."
SG_ID=$(aws ec2 create-security-group \
  --group-name "$SG_NAME" \
  --description "W03 inspection security group" \
  --vpc-id "$VPC_ID" \
  --tag-specifications "ResourceType=security-group,Tags=[{Key=course,Value=yuntech-115-1},{Key=week,Value=w03},{Key=group,Value=$GROUP},{Key=owner,Value=$OWNER}]" \
  --query "GroupId" --output text --profile "$PROFILE")

echo "SG_ID: $SG_ID"

# 加入入站規則 (TCP 22, 80 限制出口 /32)
aws ec2 authorize-security-group-ingress \
  --group-id "$SG_ID" \
  --protocol tcp --port 22 --cidr "$MY_IP_CIDR" \
  --profile "$PROFILE" >/dev/null

aws ec2 authorize-security-group-ingress \
  --group-id "$SG_ID" \
  --protocol tcp --port 80 --cidr "$MY_IP_CIDR" \
  --profile "$PROFILE" >/dev/null

# 4. 啟動 EC2 實例
echo "--> 啟動 EC2 主機..."
BLOCK_MAPPING='[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":8,"VolumeType":"gp3","Encrypted":true,"DeleteOnTermination":true}}]'
METADATA_OPTIONS='{"HttpTokens":"required","HttpEndpoint":"enabled"}'

INSTANCE_ID=$(aws ec2 run-instances \
  --image-id "$AMI_ID" \
  --instance-type t3.micro \
  --key-name "$KEY_NAME" \
  --subnet-id "$SUBNET_ID" \
  --security-group-ids "$SG_ID" \
  --associate-public-ip-address \
  --user-data "file://$USER_DATA_FILE" \
  --block-device-mappings "$BLOCK_MAPPING" \
  --metadata-options "$METADATA_OPTIONS" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=w03-inspection},{Key=course,Value=yuntech-115-1},{Key=week,Value=w03},{Key=group,Value=$GROUP},{Key=owner,Value=$OWNER}]" \
  --query "Instances[0].InstanceId" --output text --profile "$PROFILE")

echo "INSTANCE_ID: $INSTANCE_ID"

# 立即記錄資源 ID 到 .local/resources.json
cat << RES_EOF > .local/resources.json
{
  "instance_id": "$INSTANCE_ID",
  "security_group_id": "$SG_ID",
  "key_name": "$KEY_NAME",
  "commit_sha": "$COMMIT_SHA"
}
RES_EOF

echo "--> 等待實例進入 running 狀態..."
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --profile "$PROFILE"
PUBLIC_IP=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" --query "Reservations[0].Instances[0].PublicIpAddress" --output text --profile "$PROFILE")
echo "主機已啟動，公有 IP: $PUBLIC_IP"

# 記錄主機剛運行時的初次 curl 失敗狀態（任務卡要求）
echo "--> 記錄服務尚未就緒時的早期 curl 表現..."
curl -v --max-time 3 "http://$PUBLIC_IP/health" || true

echo "--> 等待 inspection 服務就緒 (/health 回傳 200)..."
HEALTH_OK=false
for i in {1..30}; do
  RESP=$(curl -s --max-time 2 "http://$PUBLIC_IP/health" || true)
  if echo "$RESP" | grep -q '"status":"ok"'; then
    echo "服務已回應: $RESP"
    if echo "$RESP" | grep -q "$COMMIT_SHA"; then
      echo "版本檢查通過！版本符合提交 $COMMIT_SHA"
      HEALTH_OK=true
      break
    fi
  fi
  sleep 5
done

if [ "$HEALTH_OK" = true ]; then
  echo "部署與驗證成功完成！"
else
  echo "警告: /health 未在預期時間內就緒，請檢查 cloud-init 或 nginx 狀態。"
fi
