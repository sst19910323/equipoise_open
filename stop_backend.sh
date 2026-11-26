#!/bin/bash
# Stop ONLY the FastAPI backend (equipoise_entry).
# 不会动 data_scheduler / horizon_sentinel; 老版 ./stop.sh 仍保留管那些.

cd /opt/equipoise

JOB="equipoise_entry"
PID_FILE="pids/$JOB.pid"
TERM_WAIT_SEC=5
UNIT="equipoise-backend.service"

echo ">>> Stopping $JOB (backend only) ..."

if command -v systemctl >/dev/null 2>&1 && systemctl list-units --all "$UNIT" --no-legend | grep -q "$UNIT"; then
    if systemctl is-active --quiet "$UNIT"; then
        systemctl stop "$UNIT"
        echo "🛑 $UNIT stopped"
    else
        systemctl reset-failed "$UNIT" >/dev/null 2>&1 || true
    fi
    rm -f "$PID_FILE"
fi

kill_pid() {
    local pid="$1"
    local label="$2"

    if ! kill -0 "$pid" 2>/dev/null; then
        return 0
    fi

    kill "$pid" 2>/dev/null
    for i in $(seq 1 $TERM_WAIT_SEC); do
        sleep 1
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "🛑 $label (PID $pid) stopped gracefully (${i}s)"
            return 0
        fi
    done

    kill -9 "$pid" 2>/dev/null
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
        echo "❌ $label (PID $pid) refused to die even with SIGKILL"
        return 1
    fi
    echo "💀 $label (PID $pid) force-killed (SIGKILL after ${TERM_WAIT_SEC}s timeout)"
    return 0
}

# Step 1: 用 PID 文件干掉记录的进程
if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE")
    if [[ -n "$PID" ]]; then
        kill_pid "$PID" "$JOB"
    fi
    rm -f "$PID_FILE"
else
    echo "ℹ️  No PID file for $JOB"
fi

# Step 2: pkill 兜底, 抓 PID 文件没记录到的孤儿进程
# 2026-10-23: 只杀真正跑后端的 venv python, 不杀命令行里恰好带 equipoise_entry 字样的别的进程
# (迁移那天 pgrep -f 把正在跑本脚本的 Fable 会话 shell 一起杀了, 后端停了没起来)
for opid in $(pgrep -f "${JOB}\.py" || true); do
    [[ "$opid" == "$$" || "$opid" == "$PPID" ]] && continue
    exe=$(readlink -f "/proc/$opid/exe" 2>/dev/null || true)
    [[ "$exe" == /opt/equipoise/venv/bin/python* ]] || continue
    echo "⚠️  Found orphan backend process $opid ($exe), stopping"
    kill_pid "$opid" "$JOB (orphan)"
done

echo ">>> Done."
