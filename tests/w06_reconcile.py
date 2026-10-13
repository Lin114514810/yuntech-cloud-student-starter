#!/usr/bin/env python3
"""Reconcile one W6 gateway run with consumer logs, PostgreSQL, and the API."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlencode
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
PREFIX_PATTERN = re.compile(r"[A-Za-z0-9-]{1,40}\Z")
REMOTE_SCRIPT = r"""#!/bin/bash
set -euo pipefail
prefix=$1
for attempt in $(seq 0 12); do
  log_counts=$(sudo journalctl -u inspection-consumer -o cat --no-pager \
    | grep -F "event_id=${prefix}-" \
    | grep -o 'result=[a-z]*' \
    | sort | uniq -c || true)
  count_for() {
    printf '%s\n' "$log_counts" | awk -v key="result=$1" '$2 == key { print $1; found=1 } END { if (!found) print 0 }'
  }
  created=$(count_for created)
  duplicate=$(count_for duplicate)
  rejected=$(count_for rejected)
  error=$(count_for error)
  db_count=$(sudo bash -c '
    set -a
    . /etc/inspection/app.env
    set +a
    PGPASSWORD="$DB_PASSWORD" psql \
      "host=$DB_HOST dbname=$DB_NAME user=$DB_USER sslmode=verify-full sslrootcert=/etc/inspection/rds-ca.pem" \
      -v "prefix=$1" -At
  ' _ "${prefix}-" <<'SQL'
SELECT count(*) FROM events WHERE left(event_id, length(:'prefix')) = :'prefix';
SQL
  )
  printf 'W06_COUNTS created=%s duplicate=%s rejected=%s error=%s db=%s\n' \
    "$created" "$duplicate" "$rejected" "$error" "$db_count"
  total=$((created + duplicate + rejected + error))
  if [ "$total" -ge 32 ] || [ "$attempt" -eq 12 ]; then
    break
  fi
  sleep 5
done
"""


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--host")
    args = parser.parse_args()
    if not PREFIX_PATTERN.fullmatch(args.code) or not PREFIX_PATTERN.fullmatch(args.run):
        parser.error("--code and --run must match ^[A-Za-z0-9-]{1,40}$")
    if len(f"{args.code}-{args.run}") > 40:
        parser.error("combined run prefix must be at most 40 characters")
    return args


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read {path.relative_to(ROOT)}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{path.relative_to(ROOT)} must contain an object")
    return value


def parse_operator_token(path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError("Cannot read .local/app.env") from exc
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip() == "OPERATOR_TOKEN":
            token = value.strip()
            if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
                token = token[1:-1]
            if token:
                return token
            break
    raise RuntimeError(".local/app.env must define OPERATOR_TOKEN")


def resolve_host(args, resources):
    host = args.host or resources.get("public_ip")
    if not isinstance(host, str) or not host:
        raise RuntimeError("No public_ip in resources.json; pass --host <current host IPv4>")
    try:
        return str(ipaddress.IPv4Address(host))
    except ipaddress.AddressValueError as exc:
        raise RuntimeError("Host must be a public IPv4 address") from exc


def request_json(url, token=None):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"HTTP request failed for {url.split('?', 1)[0]}") from exc


def get_remote_counts(host, prefix):
    key_path = Path.home() / ".ssh" / "w03_key"
    if not key_path.is_file():
        raise RuntimeError(f"SSH key not found: {key_path}")
    command = [
        "ssh",
        "-i", str(key_path),
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        f"ec2-user@{host}",
        "bash", "-s", "--", prefix,
    ]
    try:
        result = subprocess.run(
            command,
            input=REMOTE_SCRIPT,
            text=True,
            capture_output=True,
            timeout=75,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("SSH failed or timed out while reconciling the host") from exc
    if result.returncode:
        raise RuntimeError(f"Remote reconciliation failed (ssh exit {result.returncode})")
    for line in reversed(result.stdout.splitlines()):
        match = re.fullmatch(
            r"W06_COUNTS created=(\d+) duplicate=(\d+) rejected=(\d+) error=(\d+) db=(\d+)",
            line.strip(),
        )
        if match:
            keys = ("created", "duplicate", "rejected", "error", "db")
            return dict(zip(keys, map(int, match.groups())))
    raise RuntimeError("Remote reconciliation returned no count summary")


def run_reconciliation(args):
    code = args.code
    prefix = f"{code}-{args.run}"
    resources = read_json(LOCAL / "resources.json")
    host = resolve_host(args, resources)

    health = request_json(f"http://{host}/health")
    print(
        f"health version={health.get('version')} "
        f"db_configured={str(bool(health.get('db_configured'))).lower()} prefix={prefix}"
    )

    last_run = read_json(LOCAL / "w06-last-run.json")
    if last_run.get("prefix") != prefix:
        raise RuntimeError(".local/w06-last-run.json prefix does not match --code/--run")
    try:
        puback_ok = int(last_run["puback_ok"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Invalid puback_ok in .local/w06-last-run.json") from exc
    print(f"puback_ok={puback_ok}/32")

    counts = get_remote_counts(host, prefix)
    print(
        "consumer created={created} duplicate={duplicate} rejected={rejected} "
        "error={error} db={db}".format(**counts)
    )

    token = parse_operator_token(LOCAL / "app.env")
    device_id = f"{code}-d01"
    query = urlencode({"device_id": device_id})
    records = request_json(f"http://{host}/events?{query}", token)
    if not isinstance(records, list):
        raise RuntimeError("Filtered /events response was not a list")
    run_records = [
        item for item in records
        if isinstance(item, dict) and str(item.get("event_id", "")).startswith(prefix + "-")
    ]
    for item in run_records:
        print(f"filtered_event_id={item['event_id']}")
    print(f"filtered_device_id={device_id} run_count={len(run_records)}")


def main():
    args = parse_args()
    try:
        run_reconciliation(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
