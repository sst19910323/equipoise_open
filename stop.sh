#!/bin/bash
# Stop Equipoise services. 三段式杀: TERM -> wait -> KILL，再 pkill 兜底孤儿进程

cd /opt/equipoise

JOBS=("equipoise_entry" "horizon_sentinel")
TERM_WAIT_SEC=5   # 收到 TERM 后宽限时间，超时强杀

echo ">>> Stopping Equipoise Services..."

kill_pid() {
    local pid="$1"
    local name="$2"

    if ! kill -0 "$pid" 2>/dev/null; then
        return 0  # 已经不在
    fi

    kill "$pid" 2>/dev/null
    # 等它优雅退出
    for i in $(seq 1 $TERM_WAIT_SEC); do
        sleep 1
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "🛑 $name (PID $pid) stopped gracefully (${i}s)"
            return 0
        fi
    done
    # 还活着，强杀
    kill -9 "$pid" 2>/dev/null
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
        echo "❌ $name (PID $pid) refused to die even with SIGKILL"
        return 1
    fi
    echo "💀 $name (PID $pid) force-killed (SIGKILL after ${TERM_WAIT_SEC}s timeout)"
    return 0
}

for job in "${JOBS[@]}"; do
    PID_FILE="pids/$job.pid"

    # Step 1: 用 PID 文件干掉记录的进程
    if [[ -f "$PID_FILE" ]]; then
        PID=$(cat "$PID_FILE")
        if [[ -n "$PID" ]]; then
            kill_pid "$PID" "$job"
        fi
        rm -f "$PID_FILE"
    else
        echo "ℹ️  No PID file for $job"
    fi

    # Step 2: pkill 兜底 — 抓 PID 文件没记录到的孤儿进程 (例如手动 nohup 起的)
    # -f 匹配完整命令行；用 "$job.py" 避免误伤其它进程
    # 2026-10-23: 只杀真正跑该任务的 venv python, 不杀命令行里恰好带这个名字的别的进程(如正在跑本脚本的会话)
    for opid in $(pgrep -f "$job\.py" || true); do
        [[ "$opid" == "$$" || "$opid" == "$PPID" ]] && continue
        exe=$(readlink -f "/proc/$opid/exe" 2>/dev/null || true)
        [[ "$exe" == /opt/equipoise/venv/bin/python* ]] || continue
        echo "⚠️  Found orphan process $opid for $job ($exe), stopping"
        kill_pid "$opid" "$job (orphan)"
    done
done

echo ">>> Done."
