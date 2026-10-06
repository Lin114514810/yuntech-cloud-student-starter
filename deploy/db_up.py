#!/usr/bin/env python3
import ipaddress
import json
import os
from pathlib import Path
import secrets
import string
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import lab

LOCAL = ROOT / ".local"
RESOURCES_PATH = LOCAL / "resources.json"
DB_ENV_PATH = LOCAL / "db.env"
COURSE = "yuntech-115-1"
WEEK = "w05"
WAIT_SECONDS = 30 * 60


def fail(message):
    raise lab.LabError(message)


def read_resources():
    if not RESOURCES_PATH.is_file():
        fail("找不到 .local/resources.json；請先確認主機部署紀錄。")
    try:
        data = json.loads(RESOURCES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        fail(".local/resources.json 無法讀取；未進行 AWS 變更。")
    if not isinstance(data, dict) or not data.get("instance_id"):
        fail("resources.json 沒有 instance_id；未進行 AWS 變更。")
    return data


def write_resources(resources):
    LOCAL.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".resources-", dir=LOCAL)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(resources, stream, indent=2)
            stream.write("\n")
        os.replace(temp_name, RESOURCES_PATH)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def tag_spec(resource_type, tags):
    return [{"ResourceType": resource_type, "Tags": tags}]


def aws(ctx, args):
    return lab.run_aws(args, ctx["region"])


def get_instance(ctx, instance_id):
    result = aws(ctx, ["ec2", "describe-instances", "--instance-ids", instance_id])
    reservations = result.get("Reservations", [])
    instances = [item for reservation in reservations for item in reservation.get("Instances", [])]
    if len(instances) != 1:
        fail("無法以 resources.json 中的 ID 唯一確認主機；停止，沒有進行變更。")
    instance = instances[0]
    if instance.get("State", {}).get("Name") != "running":
        fail("主機目前不是 running；請先依 Lab 指示確認主機狀態後重試。")
    groups = instance.get("SecurityGroups", [])
    if len(groups) != 1:
        fail("主機必須恰有一個 Security Group，才能套用本週 DB 來源規則。")
    return instance, groups[0]["GroupId"]


def choose_subnets(ctx, vpc_id, instance_id):
    vpc = aws(ctx, ["ec2", "describe-vpcs", "--vpc-ids", vpc_id])["Vpcs"][0]
    cidrs = [
        item["CidrBlock"]
        for item in vpc.get("CidrBlockAssociationSet", [])
        if item.get("CidrBlockState", {}).get("State") == "associated"
    ]
    if not cidrs:
        cidrs = [vpc["CidrBlock"]]
    current = aws(
        ctx,
        ["ec2", "describe-subnets", "--filters", f"Name=vpc-id,Values={vpc_id}"],
    ).get("Subnets", [])
    occupied = [ipaddress.ip_network(item["CidrBlock"]) for item in current]
    azs = aws(
        ctx,
        ["ec2", "describe-availability-zones", "--filters", "Name=state,Values=available"],
    ).get("AvailabilityZones", [])
    az_ids = sorted({item["ZoneId"] for item in azs if item.get("ZoneId")})
    if len(az_ids) < 2:
        fail("此區域沒有兩個可用的不同 AZ；未進行 AWS 變更。")

    candidates = []
    for cidr in cidrs:
        network = ipaddress.ip_network(cidr)
        if network.prefixlen > 24:
            continue
        candidates.extend(
            subnet for subnet in network.subnets(new_prefix=24)
            if not any(subnet.overlaps(existing) for existing in occupied)
        )
    if len(candidates) < 2:
        fail("VPC 找不到兩段不重疊的可用 /24；未進行 AWS 變更。")

    return [
        {"cidr": str(candidates[0]), "az": az_ids[0]},
        {"cidr": str(candidates[1]), "az": az_ids[1]},
    ]


def print_plan(ctx, instance, host_sg_id, vpc_id, subnet_plan, resources):
    print(
        "\n即將建立／使用的本週資源：\n"
        f"  AWS 帳號末四碼／區域：{ctx['account'][-4:]} / {ctx['region']}\n"
        f"  VPC／EC2：{vpc_id} / {instance['InstanceId']} ({instance['State']['Name']})\n"
        f"  私有子網 A：{subnet_plan[0]['cidr']}，AZ {subnet_plan[0]['az']}\n"
        f"  私有子網 B：{subnet_plan[1]['cidr']}，AZ {subnet_plan[1]['az']}\n"
        "  路由：新路由表僅保留 VPC local；兩子網都明確關聯，不加 IGW/NAT。\n"
        f"  DB 安全群組：僅允許 TCP 5432，來源為主機 SG {host_sg_id}。\n"
        "  RDS：PostgreSQL、db.t3.micro、20 GiB gp3、加密、單一 AZ、"
        "PubliclyAccessible=false、資料庫 inspection。\n"
        "  預算：約 USD 15/月起（Free Tier、區域、備份與其他費用會影響實際金額）；"
        "執行前請確認 Learner Lab 剩餘預算及當前區域報價。\n"
        "  回收：僅依 .local/resources.json 內本週記錄的精確 ID，"
        "先保留／備份資料，再刪除本週 RDS，最後刪除本腳本建立的子網、"
        "關聯、路由表、DB 子網群組與 DB SG；不刪 VPC、主機或主機 SG。\n"
    )
    if resources.get("db_instance_identifier"):
        print(f"  已記錄的 RDS：{resources['db_instance_identifier']}（將檢查／續等，不建立第二台）")
    lab.approve("請先核對以上資源、費用、網路暴露與回收方式。", "CREATE W5 DATABASE")


def base_tags(resource_name, account):
    return [
        {"Key": "Name", "Value": resource_name},
        {"Key": "course", "Value": COURSE},
        {"Key": "week", "Value": WEEK},
        {"Key": "owner-account-suffix", "Value": account[-4:]},
    ]


def has_ownership_tags(resource, account):
    tags = {item.get("Key"): item.get("Value") for item in resource.get("Tags", [])}
    return (
        tags.get("course") == COURSE
        and tags.get("week") == WEEK
        and tags.get("owner-account-suffix") == account[-4:]
    )


def find_by_name(ctx, command, resource_key, vpc_id, name):
    result = aws(
        ctx,
        [
            "ec2", command,
            "--filters", f"Name=vpc-id,Values={vpc_id}", f"Name=tag:Name,Values={name}",
        ],
    )
    return result.get(resource_key, [])


def create_subnet(ctx, resources, instance, spec, index, account):
    key = f"db_subnet_{index}_id"
    name = f"w5-db-{instance['InstanceId'][-8:]}-{index}"
    if resources.get(key):
        subnet_id = resources[key]
        subnet = aws(ctx, ["ec2", "describe-subnets", "--subnet-ids", subnet_id])["Subnets"][0]
        if (
            subnet["VpcId"] != instance["VpcId"]
            or subnet["CidrBlock"] != spec["cidr"]
            or subnet["AvailabilityZoneId"] != spec["az"]
            or not has_ownership_tags(subnet, account)
        ):
            fail(f"resources.json 的子網 ID {subnet_id} 與本週資源不符；拒絕使用。")
    else:
        vpc_id = instance["VpcId"]
        found = find_by_name(ctx, "describe-subnets", "Subnets", vpc_id, name)
        if found:
            subnet = found[0]
            if (
                subnet["CidrBlock"] != spec["cidr"]
                or subnet["AvailabilityZoneId"] != spec["az"]
                or not has_ownership_tags(subnet, account)
            ):
                fail(f"找到名稱相同但設定不符的子網 {subnet['SubnetId']}；請人工核對，未覆寫。")
            subnet_id = subnet["SubnetId"]
        else:
            response = aws(
                ctx,
                [
                    "ec2", "create-subnet",
                    "--vpc-id", vpc_id,
                    "--cidr-block", spec["cidr"],
                    "--availability-zone-id", spec["az"],
                    "--tag-specifications",
                    json.dumps(tag_spec("subnet", base_tags(name, account))),
                ],
            )
            subnet_id = response["Subnet"]["SubnetId"]
            resources[key] = subnet_id
            write_resources(resources)
    aws(ctx, ["ec2", "modify-subnet-attribute", "--subnet-id", subnet_id, "--no-map-public-ip-on-launch"])
    resources[key] = subnet_id
    write_resources(resources)
    return subnet_id


def create_route_table(ctx, resources, instance, account):
    key = "db_route_table_id"
    vpc_id = instance["VpcId"]
    if resources.get(key):
        route_table_id = resources[key]
    else:
        name = f"w5-db-{instance['InstanceId'][-8:]}"
        found = find_by_name(ctx, "describe-route-tables", "RouteTables", vpc_id, name)
        if found:
            route_table_id = found[0]["RouteTableId"]
        else:
            response = aws(
                ctx,
                [
                    "ec2", "create-route-table",
                    "--vpc-id", vpc_id,
                    "--tag-specifications",
                    json.dumps(tag_spec("route-table", base_tags(name, account))),
                ],
            )
            route_table_id = response["RouteTable"]["RouteTableId"]
            resources[key] = route_table_id
            write_resources(resources)
    resources[key] = route_table_id
    write_resources(resources)
    table = aws(ctx, ["ec2", "describe-route-tables", "--route-table-ids", route_table_id])
    route_table = table["RouteTables"][0]
    routes = route_table.get("Routes", [])
    if (
        route_table.get("VpcId") != vpc_id
        or len(routes) != 1
        or routes[0].get("GatewayId") != "local"
        or not has_ownership_tags(route_table, account)
    ):
        fail(f"路由表 {route_table_id} 不只含 local 路由；拒絕繼續。")
    return route_table_id


def associate_subnet(ctx, resources, subnet_id, route_table_id, index):
    key = f"db_route_association_{index}_id"
    tables = aws(ctx, ["ec2", "describe-route-tables", "--route-table-ids", route_table_id])["RouteTables"]
    associations = [
        item for item in tables[0].get("Associations", [])
        if item.get("SubnetId") == subnet_id
    ]
    if associations:
        association_id = associations[0]["RouteTableAssociationId"]
        if resources.get(key) and resources[key] != association_id:
            fail(f"resources.json 的路由關聯 ID 與讀回不符：{key}。")
    else:
        if resources.get(key):
            fail(f"記錄的路由關聯 {resources[key]} 不在預期路由表；拒絕重新關聯。")
        response = aws(
            ctx,
            [
                "ec2", "associate-route-table",
                "--route-table-id", route_table_id,
                "--subnet-id", subnet_id,
            ],
        )
        association_id = response["AssociationId"]
    resources[key] = association_id
    write_resources(resources)
    return association_id


def create_db_security_group(ctx, resources, instance, host_sg_id, account):
    key = "db_security_group_id"
    name = f"w5-db-{instance['InstanceId'][-8:]}"
    if resources.get(key):
        group_id = resources[key]
    else:
        found = aws(
            ctx,
            [
                "ec2", "describe-security-groups",
                "--filters", f"Name=vpc-id,Values={instance['VpcId']}", f"Name=group-name,Values={name}",
            ],
        ).get("SecurityGroups", [])
        if found:
            group = found[0]
            if not has_ownership_tags(group, account):
                fail(f"同名安全群組 {group['GroupId']} 不屬於本週資源；拒絕沿用。")
            group_id = group["GroupId"]
        else:
            response = aws(
                ctx,
                [
                    "ec2", "create-security-group",
                    "--group-name", name,
                    "--description", "W5 private PostgreSQL source is the inspection host SG",
                    "--vpc-id", instance["VpcId"],
                    "--tag-specifications",
                    json.dumps(tag_spec("security-group", base_tags(name, account))),
                ],
            )
            group_id = response["GroupId"]
            resources[key] = group_id
            write_resources(resources)

    group = aws(ctx, ["ec2", "describe-security-groups", "--group-ids", group_id])["SecurityGroups"][0]
    if group.get("VpcId") != instance["VpcId"] or not has_ownership_tags(group, account):
        fail(f"安全群組 {group_id} 缺少本週擁有權標籤；拒絕修改。")
    ingress = group.get("IpPermissions", [])
    if not ingress:
        aws(
            ctx,
            [
                "ec2", "authorize-security-group-ingress",
                "--group-id", group_id,
                "--protocol", "tcp",
                "--port", "5432",
                "--source-group", host_sg_id,
            ],
        )
        group = aws(ctx, ["ec2", "describe-security-groups", "--group-ids", group_id])["SecurityGroups"][0]
        ingress = group.get("IpPermissions", [])
    if len(ingress) != 1 or (
        ingress[0].get("IpProtocol") != "tcp"
        or ingress[0].get("FromPort") != 5432
        or ingress[0].get("ToPort") != 5432
        or [entry.get("GroupId") for entry in ingress[0].get("UserIdGroupPairs", [])] != [host_sg_id]
        or ingress[0].get("IpRanges")
        or ingress[0].get("Ipv6Ranges")
        or ingress[0].get("PrefixListIds")
    ):
        fail(f"DB SG {group_id} 的入站規則不是唯一的 TCP 5432／主機 SG 規則；拒絕繼續。")
    egress = group.get("IpPermissionsEgress", [])
    if egress:
        aws(
            ctx,
            [
                "ec2", "revoke-security-group-egress",
                "--group-id", group_id,
                "--ip-permissions", json.dumps(egress),
            ],
        )
    resources[key] = group_id
    write_resources(resources)
    return group_id


def create_db_subnet_group(ctx, resources, instance, subnet_ids, account):
    key = "db_subnet_group_name"
    name = f"w5-db-{instance['InstanceId'][-8:]}"
    group_name = resources.get(key, name)
    existing = aws(ctx, ["rds", "describe-db-subnet-groups"]).get("DBSubnetGroups", [])
    match = next((item for item in existing if item.get("DBSubnetGroupName") == group_name), None)
    if match:
        tags = aws(
            ctx,
            ["rds", "list-tags-for-resource", "--resource-name", match["DBSubnetGroupArn"]],
        ).get("TagList", [])
        match_tags = {item.get("Key"): item.get("Value") for item in tags}
        if (
            match_tags.get("course") != COURSE
            or match_tags.get("week") != WEEK
            or match_tags.get("owner-account-suffix") != account[-4:]
        ):
            fail(f"DB 子網群組 {group_name} 缺少本週擁有權標籤；拒絕沿用。")
        existing_ids = sorted(item["SubnetIdentifier"] for item in match.get("Subnets", []))
        if existing_ids != sorted(subnet_ids):
            fail(f"既有 DB 子網群組 {group_name} 的子網不符；拒絕沿用。")
    else:
        aws(
            ctx,
            [
                "rds", "create-db-subnet-group",
                "--db-subnet-group-name", name,
                "--db-subnet-group-description", "W5 private PostgreSQL subnets",
                "--subnet-ids", *subnet_ids,
                "--tags", json.dumps(base_tags(name, account)),
            ],
        )
    resources[key] = group_name
    write_resources(resources)
    return group_name


def generate_password():
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(48))


def write_db_env(password, host=""):
    LOCAL.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = f"DB_HOST={host}\nDB_NAME=inspection\nDB_USER=inspection_admin\nDB_PASSWORD={password}\n"
    fd, temp_name = tempfile.mkstemp(prefix=".db-env-", dir=LOCAL)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temp_name, DB_ENV_PATH)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def existing_password():
    if not DB_ENV_PATH.exists():
        return None
    if DB_ENV_PATH.stat().st_mode & 0o777 != 0o600:
        fail(".local/db.env 已存在但權限不是 600；請先人工核對，不覆寫。")
    values = {}
    for line in DB_ENV_PATH.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if values.get("DB_USER") == "inspection_admin" and values.get("DB_NAME") == "inspection" and values.get("DB_PASSWORD"):
        return values["DB_PASSWORD"]
    fail(".local/db.env 已存在且不是本週資料庫設定；請先人工核對，不覆寫。")


def create_database(ctx, resources, db_subnet_group, db_sg_id, instance, account):
    key = "db_instance_identifier"
    if resources.get(key):
        if existing_password() is None:
            fail("已記錄 RDS，但找不到 .local/db.env 密碼；不要重建資料庫，先從安全備份復原秘密。")
        return resources[key]
    identifier = f"inspection-w5-{instance['InstanceId'][-8:].lower()}"
    listed = aws(ctx, ["rds", "describe-db-instances"]).get("DBInstances", [])
    existing = next((item for item in listed if item.get("DBInstanceIdentifier") == identifier), None)
    if existing:
        tags = aws(
            ctx,
            ["rds", "list-tags-for-resource", "--resource-name", existing["DBInstanceArn"]],
        ).get("TagList", [])
        if not any(tag.get("Key") == "week" and tag.get("Value") == WEEK for tag in tags):
            fail(f"同名 RDS {identifier} 不屬於本週資源；拒絕沿用。")
        if existing_password() is None:
            fail("已找到本週 RDS，但找不到 .local/db.env 密碼；請先復原秘密，未建立第二台。")
        resources[key] = identifier
        write_resources(resources)
        return identifier

    password = existing_password() or generate_password()
    write_db_env(password)
    request = {
        "DBInstanceIdentifier": identifier,
        "AllocatedStorage": 20,
        "DBInstanceClass": "db.t3.micro",
        "Engine": "postgres",
        "MasterUsername": "inspection_admin",
        "MasterUserPassword": password,
        "DBName": "inspection",
        "VpcSecurityGroupIds": [db_sg_id],
        "DBSubnetGroupName": db_subnet_group,
        "StorageType": "gp3",
        "StorageEncrypted": True,
        "PubliclyAccessible": False,
        "MultiAZ": False,
        "Tags": base_tags(identifier, account),
    }
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="w5-rds-", suffix=".json") as stream:
        os.chmod(stream.name, 0o600)
        json.dump(request, stream)
        stream.flush()
        aws(ctx, ["rds", "create-db-instance", "--cli-input-json", f"file://{stream.name}"])
    resources[key] = identifier
    write_resources(resources)
    return identifier


def wait_for_database(ctx, identifier, subnet_group, db_sg_id, account):
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        result = aws(ctx, ["rds", "describe-db-instances", "--db-instance-identifier", identifier])
        instances = result.get("DBInstances", [])
        if len(instances) != 1:
            fail(f"讀回 RDS {identifier} 失敗；請保留資源並檢查 AWS 狀態。")
        db = instances[0]
        status = db.get("DBInstanceStatus", "unknown")
        print(f"RDS 狀態：{status}")
        if status == "available":
            if db.get("PubliclyAccessible") is not False:
                fail("RDS 讀回 PubliclyAccessible 不是 false；停止後續操作並檢查安全設定。")
            if not db.get("Endpoint", {}).get("Address"):
                fail("RDS available 但缺少 endpoint；保留資源並檢查 AWS 狀態。")
            expected_tags = {
                "course": COURSE,
                "week": WEEK,
                "owner-account-suffix": account[-4:],
            }
            tags = aws(
                ctx,
                ["rds", "list-tags-for-resource", "--resource-name", db["DBInstanceArn"]],
            ).get("TagList", [])
            if not all(
                {item.get("Key"): item.get("Value") for item in tags}.get(key) == value
                for key, value in expected_tags.items()
            ):
                fail(f"RDS {identifier} 缺少本週擁有權標籤；拒絕使用。")
            if (
                db.get("Engine") != "postgres"
                or db.get("DBInstanceClass") != "db.t3.micro"
                or db.get("AllocatedStorage") != 20
                or db.get("StorageType") != "gp3"
                or db.get("StorageEncrypted") is not True
                or db.get("MultiAZ") is not False
                or db.get("DBSubnetGroup", {}).get("DBSubnetGroupName") != subnet_group
                or [item.get("VpcSecurityGroupId") for item in db.get("VpcSecurityGroups", [])] != [db_sg_id]
            ):
                fail(f"RDS {identifier} 的讀回設定與本週規格不符；拒絕部署服務。")
            return db
        if status in {"failed", "inaccessible-encryption-credentials", "incompatible-restore"}:
            fail(f"RDS 進入 {status}；保留已建立資源，請先診斷，不要重跑建立流程。")
        if time.monotonic() >= deadline:
            fail(f"等待 {identifier} 超過 30 分鐘；請檢查資源狀態，不要建立第二台。")
        time.sleep(30)


def run():
    ctx = lab.verify()
    resources = read_resources()
    instance, host_sg_id = get_instance(ctx, resources["instance_id"])
    vpc_id = instance.get("VpcId")
    if not vpc_id:
        fail("主機讀回缺少 VPC ID；停止。")
    subnet_plan = resources.get("db_subnet_plan") or choose_subnets(ctx, vpc_id, instance["InstanceId"])
    print_plan(ctx, instance, host_sg_id, vpc_id, subnet_plan, resources)
    if not resources.get("db_subnet_plan"):
        resources["db_subnet_plan"] = subnet_plan
        write_resources(resources)

    account = ctx["account"]
    subnet_ids = [
        create_subnet(ctx, resources, instance, subnet_plan[0], "a", account),
        create_subnet(ctx, resources, instance, subnet_plan[1], "b", account),
    ]
    route_table_id = create_route_table(ctx, resources, instance, account)
    for index, subnet_id in enumerate(subnet_ids, start=1):
        associate_subnet(ctx, resources, subnet_id, route_table_id, index)
    db_sg_id = create_db_security_group(ctx, resources, instance, host_sg_id, account)
    subnet_group = create_db_subnet_group(ctx, resources, instance, subnet_ids, account)
    identifier = create_database(ctx, resources, subnet_group, db_sg_id, instance, account)
    db = wait_for_database(ctx, identifier, subnet_group, db_sg_id, account)

    write_db_env(existing_password(), db["Endpoint"]["Address"])
    resources["db_endpoint"] = db["Endpoint"]["Address"]
    resources["db_status"] = db["DBInstanceStatus"]
    resources["db_publicly_accessible"] = db["PubliclyAccessible"]
    write_resources(resources)
    print(
        f"完成：{identifier} status={db['DBInstanceStatus']} "
        f"PubliclyAccessible={str(db['PubliclyAccessible']).lower()}。"
    )
    print("資料庫密碼只存於權限 600 的 .local/db.env；未顯示。")


if __name__ == "__main__":
    try:
        run()
    except lab.LabError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        raise SystemExit(1)
