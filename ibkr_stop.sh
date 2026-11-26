#!/usr/bin/env bash
# Stop IB Gateway for mobile/TWS login. Keep it stopped until an explicit start.
set -euo pipefail

SERVICE=ibgateway.service
PAT='ibcalpha|ibgateway|gatewaystart'

systemctl stop "$SERVICE"
sleep 2
if pgrep -fai "$PAT"; then
    echo "FATAL: $SERVICE stopped but a Gateway/IBC process escaped its cgroup" >&2
    exit 3
fi
echo "IB Gateway stopped; no API process remains."
