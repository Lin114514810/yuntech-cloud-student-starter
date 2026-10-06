#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

bash scripts/verify-aws.sh
exec python3 deploy/db_up.py
