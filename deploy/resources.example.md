# W3 個人資源清單（填寫範本，每人一份）

帳號末四碼：___；region：___；組名：___；組內代號：___；部署 commit：___。
既有預設 VPC／子網／IGW 僅引用，不能刪除。

| 資源類型 | 本次建立的 ID | UTC 建立時間 | 擁有權標籤／關聯證據 | 精確回收方式 | 回收後讀回與時間 |
|---|---|---|---|---|---|
| EC2 instance | 待填 | | | 以 ID terminate | |
| 根 EBS | 待填 | | instance 關聯、DeleteOnTermination | 隨終止刪除，仍需讀回 | |
| ENI | 待填 | | instance 關聯 | 隨終止刪除，仍需讀回 | |
| Security group | 待填 | | course/week/group | 確認 ENI 已清除後，以 ID 刪除 | |
| 匯入的 EC2 key pair | 待填 | | 自己生成的公鑰對應 | 以 ID 刪除 | |
| SSM instance-profile 關聯（若測） | 待填 | | 既有 LabInstanceProfile | 解除本次關聯，不刪 profile | |

重建時另記第二輪的新 ID，不能以名稱當作刪除條件。第二輪主機本週結束時停止保留，在「回收後讀回與時間」欄改寫「保留（stopped），下週 W4 處理」。報告不附完整帳號、憑證、私鑰或簽名網址。

## W5 私有 PostgreSQL 資源

`deploy/db-up.sh` 會把實際建立的 ID 追加到同一份 `.local/resources.json`。結束本週時停止保留主機與 RDS，不刪除資料庫；將實測 ID、UTC 時間及停止後讀回結果填入自己的清單。

| 資源類型 | 本次建立的 ID／名稱 | UTC 建立時間 | 擁有權標籤／關聯證據 | 本週處理與未來精確回收方式 | 停止／回收後讀回與時間 |
|---|---|---|---|---|---|
| RDS PostgreSQL | `db_instance_identifier` | | course/week/owner-account-suffix；private 子網群組與 DB SG | 本週保留並停止；未來先備份，再以此識別碼刪除 RDS | |
| RDS endpoint | `db_endpoint` | | 對應上列 DB instance | 非獨立資源；不得當成刪除識別碼 | |
| DB subnet group | `db_subnet_group_name` | | 僅含本週兩個私有 subnet ID | 確認 RDS 已刪除後，以名稱刪除 | |
| DB security group | `db_security_group_id` | | ingress 僅 TCP 5432，來源為本週主機 SG | 確認 RDS/ENI 已不再使用後，以 ID 刪除 | |
| 私有子網 A／B | `db_subnet_a_id`、`db_subnet_b_id` | | 兩個不重疊 `/24`、不同 AZ，皆關聯本週 route table | 確認無資源使用後，逐一以 ID 刪除 | |
| 子網路由關聯 A／B | `db_route_association_1_id`、`db_route_association_2_id` | | 關聯至本週 route table | 子網刪除前，以各關聯 ID 解除 | |
| 私有路由表 | `db_route_table_id` | | 只含 `local`；不含 IGW/NAT default route | 先解除本週子網關聯，再以 ID 刪除 | |

`.local/db.env` 是密碼秘密檔（權限 600），不放入繳交清單或 Git。清單只記資源識別資料；刪除前再次讀回並核對擁有權，禁止用名稱或標籤做廣泛刪除。
