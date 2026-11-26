#!/bin/bash
# Start ONLY the FastAPI backend (equipoise_entry).
# 数据下载 + 辩论调度 (data_scheduler / horizon_sentinel) 现在由 Claude Code 手动触发.
# 老版 ./start.sh 一次起三个的脚本仍保留, 不互相干扰.

cd /opt/equipoise

VENV=./venv/bin/python
JOB="equipoise_entry"
API_PORT=8001
HEALTH_WAIT_SEC=3
UNIT="equipoise-backend.service"

mkdir -p logs pids

PID_FILE="pids/$JOB.pid"

echo ">>> Starting $JOB (backend only) ..."

# Prefer systemd when available. Starting from automation shells with plain
# nohup can leave the backend tied to the caller's process group.
if command -v systemctl >/dev/null 2>&1 && command -v systemd-run >/dev/null 2>&1; then
    if systemctl is-active --quiet "$UNIT"; then
        echo "⚠️  $JOB already running under $UNIT. Skip."
        echo "   Run ./stop_backend.sh first to restart."
        exit 0
    fi
    systemd-run --unit "${UNIT%.service}" \
        --property=WorkingDirectory=/opt/equipoise \
        --property=Restart=always \
        --property=RestartSec=3 \
        /opt/equipoise/venv/bin/python -u /opt/equipoise/equipoise_entry.py >/dev/null
    sleep "$HEALTH_WAIT_SEC"
    if systemctl is-active --quiet "$UNIT"; then
        MAIN_PID=$(systemctl show -p MainPID --value "$UNIT")
        echo "$MAIN_PID" > "$PID_FILE"
        echo "✅ Started $JOB under $UNIT (PID: $MAIN_PID) - listening on :$API_PORT"
        echo ">>> Done."
        exit 0
    fi
    echo "❌ $JOB failed to stay active under $UNIT. Status:"
    systemctl status "$UNIT" --no-pager || true
    exit 1
fi

# Step 1: 检查是否已在跑 (PID 文件 + 实际进程双重确认)
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "⚠️  $JOB already running (PID: $(cat "$PID_FILE")). Skip."
    echo "   Run ./stop_backend.sh first to restart."
    exit 0
fi

# 删掉残留过期 PID 文件
[[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"

# Step 2: 端口空闲检查 (防止 zombie 进程占住 8001 导致新进程秒崩)
if ss -ltn 2>/dev/null | grep -q ":${API_PORT}\b" || \
   netstat -ltn 2>/dev/null | grep -q ":${API_PORT}\b"; then
    echo "❌ NOT started: port $API_PORT is already bound by another process"
    echo "   Find owner: ss -ltnp | grep $API_PORT  (or lsof -i:$API_PORT)"
    exit 1
fi

# Step 3: 启动
nohup $VENV -u "$JOB.py" > "logs/$JOB.log" 2>&1 &
PID=$!
echo "$PID" > "$PID_FILE"

# Step 4: 立即验证 - 给 ${HEALTH_WAIT_SEC}s 启动时间, 再确认进程还活着
sleep $HEALTH_WAIT_SEC
if kill -0 "$PID" 2>/dev/null; then
    echo "✅ Started $JOB (PID: $PID) - listening on :$API_PORT"
else
    rm -f "$PID_FILE"
    echo "❌ $JOB died within ${HEALTH_WAIT_SEC}s of startup. Tail of logs/$JOB.log:"
    echo "----"
    tail -20 "logs/$JOB.log"
    echo "----"
    exit 1
fi

echo ">>> Done."
