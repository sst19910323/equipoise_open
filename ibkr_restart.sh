#!/usr/bin/env bash
# ibkr_restart.sh — IB Gateway 唯一合法启动/重启规程
#
# 铁律:
#   ① systemd 是唯一进程所有者；禁止 nohup/手工拉起第二棵树。
#   ② 全程只允许一棵 Gateway/IBC 进程树 (双树 = 重复通知 + 会话互踢)。
#   ③ 只试一次。2FA 没批/没起来 = 明确失败退出, 绝不循环重试 (防 2FA 轰炸 + 防锁号)。
#   ④ 用户在手机上批 2FA 这个动作本身 = 人批准了这次重启 (人在环上的闸)。
#
# 用法 (服务器): cd /opt/equipoise && bash ibkr_restart.sh
# 退出码: 0=重启成功且探针全通 / 1=端口没起来(多半2FA没批) / 2=起来了但探针报数据死 / 3=进程杀不干净
set -u

EQUIPOISE=/opt/equipoise
SERVICE=ibgateway.service
PORT="${EQUIPOISE_IBKR_PORT:-4001}"
WAIT_2FA=240                                   # 秒: 等手机批 2FA + Gateway 起监听
PAT='ibcalpha|ibgateway|gatewaystart'          # Gateway/IBC 进程树特征 (大小写不敏感; Xvfb 不在内, 不杀)

echo "== [1/4] 当前 Gateway/IBC 进程树 =="
pgrep -fai "$PAT" || echo "(无)"

echo "== [2/4] 由 systemd 停掉完整 cgroup (铁律①②) =="
systemctl stop "$SERVICE"
sleep 3
if pgrep -fi "$PAT" >/dev/null; then
    echo "FATAL: systemd 停止后仍有失管进程, 不冒险启动第二棵树:"; pgrep -fai "$PAT"; exit 3
fi
echo "已杀干净 (单树确认)"

# Xvfb :1 保活 (IBC 需要虚拟显示; 它不属于 Gateway 树, 只补不杀)
if ! pgrep -f "Xvfb :1" >/dev/null; then
    echo "== Xvfb :1 不在, 拉起 =="
    nohup Xvfb :1 -screen 0 1024x768x24 >/dev/null 2>&1 &
    sleep 2
fi

echo "== [3/4] 由 systemd 全新拉起 Gateway =="
systemctl start "$SERVICE"

echo ""
echo "############################################################"
echo "##  2FA 即将推送到手机 IBKR App —— 现在去点批准!          ##"
echo "##  窗口 ${WAIT_2FA}s, 超时 Gateway 自杀 (TWOFA_TIMEOUT_ACTION=exit) ##"
echo "############################################################"
echo ""

echo "== [4/4] 等端口 $PORT 监听 =="
elapsed=0
while [ "$elapsed" -lt "$WAIT_2FA" ]; do
    sleep 15; elapsed=$((elapsed + 15))
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":${PORT}$"; then
        echo "端口已监听 (${elapsed}s), 稳 5s 后跑探针验证..."
        sleep 5
        EQUIPOISE_IBKR_PORT="$PORT" "$EQUIPOISE/venv/bin/python" "$EQUIPOISE/ibkr_probe.py"
        exit $?
    fi
    echo "  ...等待中 (${elapsed}s/${WAIT_2FA}s; 手机还没批 2FA 就快去)"
done

echo ""
echo "FAIL: ${WAIT_2FA}s 内端口没起来 —— 多半 2FA 没批准或登录失败。"
echo "本脚本只试一次 (铁律③)。"
echo "日志: journalctl -u $SERVICE -n 200 --no-pager"
echo "人确认好了(手机在手/网络正常)再重跑: bash ibkr_restart.sh"
exit 1
