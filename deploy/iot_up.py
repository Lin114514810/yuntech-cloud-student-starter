#!/usr/bin/env python3
"""Provision two narrowly scoped AWS IoT certificate identities for W6."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import lab

LOCAL = ROOT / ".local"
RESOURCES_PATH = LOCAL / "resources.json"
IOT_DIR = LOCAL / "iot"
CODE_PATTERN = re.compile(r"[A-Za-z0-9-]{1,40}\Z")
POLICY_VERSION = "2012-10-17"


def fail(message):
    raise lab.LabError(message)


def verify_aws():
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "verify-aws.sh")],
        cwd=ROOT,
        check=False,
    )
    if result.returncode:
        fail("Learner Lab identity verification failed; no IoT resources were changed.")
    return lab.context()


def read_resources():
    if LOCAL.is_symlink() or not RESOURCES_PATH.is_file() or RESOURCES_PATH.is_symlink():
        fail("找不到安全的 .local/resources.json；請先完成主機資源設定。")
    try:
        resources = json.loads(RESOURCES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        fail(".local/resources.json 無法讀取；未進行 AWS 變更。")
    if not isinstance(resources, dict):
        fail(".local/resources.json 必須是 JSON object。")
    return resources


def write_resources(resources):
    LOCAL.mkdir(mode=0o700, parents=True, exist_ok=True)
    if RESOURCES_PATH.is_symlink():
        fail("拒絕覆寫 symlink .local/resources.json。")
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


def write_new_secret(path, value):
    if path.exists() or path.is_symlink():
        fail(f"Refusing to overwrite existing IoT identity file: {path.name}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(value)


def atomic_write(path, value, mode=0o600):
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def policy_document(identity, code, account, region):
    client_id = f"{identity}-{code}"
    topic = f"cxe/{code}/gw/events"
    statements = [
        {
            "Effect": "Allow",
            "Action": ["iot:Connect"],
            "Resource": [f"arn:aws:iot:{region}:{account}:client/{client_id}"],
        }
    ]
    if identity == "gw":
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["iot:Publish"],
                "Resource": [f"arn:aws:iot:{region}:{account}:topic/{topic}"],
            }
        )
    else:
        statements.extend(
            [
                {
                    "Effect": "Allow",
                    "Action": ["iot:Subscribe"],
                    "Resource": [f"arn:aws:iot:{region}:{account}:topicfilter/{topic}"],
                },
                {
                    "Effect": "Allow",
                    "Action": ["iot:Receive"],
                    "Resource": [f"arn:aws:iot:{region}:{account}:topic/{topic}"],
                },
            ]
        )
    return {"Version": POLICY_VERSION, "Statement": statements}


def aws(ctx, args):
    return lab.run_aws(args, ctx["region"])


def ensure_certificate(ctx, resources, identity):
    key = f"iot_{identity}_cert_id"
    cert_path = IOT_DIR / f"{identity}.cert.pem"
    private_path = IOT_DIR / f"{identity}.private.key"
    cert_id = resources.get(key)
    if cert_id:
        if any(
            path.is_symlink()
            or not path.is_file()
            or (path.stat().st_mode & 0o777) != 0o600
            for path in (cert_path, private_path)
        ):
            fail(f"{identity} certificate ID exists but local key files are missing; refusing to create a replacement.")
        return cert_id

    if cert_path.exists() or private_path.exists():
        fail(f"Untracked {identity} certificate files exist; inspect manually before continuing.")
    result = aws(ctx, ["iot", "create-keys-and-certificate", "--set-as-active"])
    cert_id = result.get("certificateId")
    cert_pem = result.get("certificatePem")
    private_key = result.get("keyPair", {}).get("PrivateKey")
    if not all(isinstance(value, str) and value for value in (cert_id, cert_pem, private_key)):
        fail(f"AWS returned incomplete {identity} certificate data; inspect AWS IoT before retrying.")

    resources[key] = cert_id
    write_resources(resources)
    write_new_secret(cert_path, cert_pem)
    write_new_secret(private_path, private_key)
    return cert_id


def ensure_policy(ctx, resources, identity, code):
    key = f"iot_{identity}_policy"
    name = f"cxe-{code}-{identity}"
    expected = policy_document(identity, code, ctx["account"], ctx["region"])
    if resources.get(key):
        if resources[key] != name:
            fail(f"{key} contains an unexpected policy name; refusing to change it.")
        response = aws(ctx, ["iot", "get-policy", "--policy-name", name])
        document = response.get("policy", {}).get("policyDocument")
        try:
            actual = json.loads(document) if isinstance(document, str) else document
        except json.JSONDecodeError:
            actual = None
        if actual != expected:
            fail(f"Existing policy {name} differs from the required exact-scope policy.")
        return name

    try:
        response = aws(ctx, ["iot", "get-policy", "--policy-name", name])
    except lab.LabError as exc:
        if "ResourceNotFoundException" not in str(exc):
            raise
        try:
            aws(
                ctx,
                [
                    "iot", "create-policy",
                    "--policy-name", name,
                    "--policy-document", json.dumps(expected, separators=(",", ":")),
                ],
            )
        except lab.LabError as create_error:
            fail(f"Could not create policy {name}: {create_error}")
    else:
        document = response.get("policy", {}).get("policyDocument")
        try:
            actual = json.loads(document) if isinstance(document, str) else document
        except json.JSONDecodeError:
            actual = None
        if actual != expected:
            fail(f"Untracked policy {name} already exists with a different document.")

    resources[key] = name
    write_resources(resources)
    return name


def ensure_attached(ctx, policy_name, cert_id):
    target = f"arn:aws:iot:{ctx['region']}:{ctx['account']}:cert/{cert_id}"
    attached = aws(ctx, ["iot", "list-attached-policies", "--target", target])
    if any(item.get("policyName") == policy_name for item in attached.get("policies", [])):
        return
    aws(
        ctx,
        [
            "iot", "attach-policy",
            "--policy-name", policy_name,
            "--target", target,
        ],
    )


def download_root_ca():
    path = IOT_DIR / "AmazonRootCA1.pem"
    if path.is_file():
        if path.is_symlink() or (path.stat().st_mode & 0o777) != 0o600:
            fail("Existing AmazonRootCA1.pem must be a regular file with mode 600.")
        return
    if path.exists() or path.is_symlink():
        fail("Invalid existing AmazonRootCA1.pem path; inspect it before retrying.")
    try:
        with urlopen("https://www.amazontrust.com/repository/AmazonRootCA1.pem", timeout=15) as response:
            content = response.read().decode("ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise lab.LabError("Could not download Amazon Root CA 1 over HTTPS.") from exc
    if "-----BEGIN CERTIFICATE-----" not in content:
        fail("Downloaded Root CA did not contain a PEM certificate.")
    write_new_secret(path, content)


def print_plan(code, ctx, resources):
    missing = [
        identity for identity in ("gw", "consumer")
        if not resources.get(f"iot_{identity}_cert_id")
    ]
    print(
        "\nW6 AWS IoT 變更計畫：\n"
        f"  Learner Lab 帳號末四碼／區域：{ctx['account'][-4:]} / {ctx['region']}\n"
        f"  代號／MQTT 主題：{code} / cxe/{code}/gw/events\n"
        f"  憑證：新建 {len(missing)} 張（閘道／consumer 各一；已記錄的 ID 會沿用）。\n"
        f"  政策：cxe-{code}-gw、cxe-{code}-consumer；僅允許各自 client ID 與指定 topic。\n"
        "  網路：只建立 IoT Core 憑證／政策；不改 VPC、Security Group、主機埠或 IAM。\n"
        "  費用：不建立運算或資料庫資源；IoT Core 訊息依用量計費，請先核對 Learner Lab 預算／Free Tier。\n"
        "  回收：以 resources.json 的憑證 ID 停用並刪除憑證；先解除 policy attachment，再刪除兩個精確政策名稱。\n"
        "  本機私鑰：留在 .local/iot（目錄 700、檔案 600）；確認憑證已不再使用後才手動回收。\n"
    )
    lab.approve("請核對精確資源、費用、網路暴露與回收方式。", "CREATE W6 IOT")


def provision(code):
    ctx = verify_aws()
    resources = read_resources()
    recorded_code = resources.get("iot_code")
    if recorded_code and recorded_code != code:
        fail("resources.json already records a different iot_code; refusing to mix identities.")
    if resources.get("iot_gw_policy") not in (None, f"cxe-{code}-gw"):
        fail("Existing gateway policy key does not match --code.")
    if resources.get("iot_consumer_policy") not in (None, f"cxe-{code}-consumer"):
        fail("Existing consumer policy key does not match --code.")
    if IOT_DIR.is_symlink():
        fail("Refusing symlink .local/iot directory.")
    print_plan(code, ctx, resources)
    LOCAL.mkdir(mode=0o700, parents=True, exist_ok=True)
    IOT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if IOT_DIR.is_symlink():
        fail("Refusing symlink .local/iot directory.")
    os.chmod(IOT_DIR, 0o700)
    resources["iot_code"] = code
    write_resources(resources)

    endpoint = aws(ctx, ["iot", "describe-endpoint", "--endpoint-type", "iot:Data-ATS"])
    endpoint_address = endpoint.get("endpointAddress")
    if not isinstance(endpoint_address, str) or not endpoint_address:
        fail("AWS IoT returned no data endpoint.")
    atomic_write(IOT_DIR / "endpoint", endpoint_address + "\n")

    cert_ids = {
        identity: ensure_certificate(ctx, resources, identity)
        for identity in ("gw", "consumer")
    }
    policy_names = {
        identity: ensure_policy(ctx, resources, identity, code)
        for identity in ("gw", "consumer")
    }
    for identity in ("gw", "consumer"):
        ensure_attached(ctx, policy_names[identity], cert_ids[identity])
    download_root_ca()

    for identity, cert_id in cert_ids.items():
        result = aws(ctx, ["iot", "describe-certificate", "--certificate-id", cert_id])
        status = result.get("certificateDescription", {}).get("status")
        if status != "ACTIVE":
            fail(f"{identity} certificate status is {status or 'unknown'}, expected ACTIVE.")
        print(f"{identity} certificate status={status}")

    print(f"IoT endpoint saved; resources recorded for code={code}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", required=True, help="Your group-member code, e.g. g03-m2")
    args = parser.parse_args()
    if not CODE_PATTERN.fullmatch(args.code):
        parser.error("--code must contain only ASCII letters, digits, and hyphens (1-40 chars)")
    try:
        provision(args.code)
    except lab.LabError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
