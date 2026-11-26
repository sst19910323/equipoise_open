#!/usr/bin/env bash
# duty.sh — 值班循环的一次呼吸(Hermes 或 cron 每 breath_minutes 跑一次; 可信层, 不进沙箱)。
#   1. scripts/duty_status.py → 退出码非 0 就结束(1 没事 / 2 盈透挂 / 3 没额度 / 4 池满)。
#   2. 退出码 0 → 用推荐身份的 codex 起一个短会话(.codex/duty.md), 让它写决定文件。
#   3. 核对决定(scripts/duty_record.py verify: 只许该市场里没在跑的标的、市场在数据窗内、不超池子空位、身份一致、推迟不超上限;
#      更新轮空计数; 记 PG duty_events) → systemd-run 现成 dispatcher 起后台单元(每个单元记一行 launch)。
# 记忆在 PG(duty_events), 文件只是排障日志; 前端 /api/duty。规则不在这里, 在 duty_record.py / duty_status.py / config/duty.yaml。
# 用法: cd /opt/equipoise && bash scripts/duty.sh        (日志 logs/duty.log)
set -u
cd "$(dirname "$0")/.." || exit 1
ROOT=$(pwd)
PY="$ROOT/venv/bin/python"
TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG="$ROOT/logs/duty.log"; mkdir -p "$ROOT/logs" "$ROOT/data/status"
say() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG"; }

# 同一时刻只允许一次呼吸
exec 9>"/run/lock/equipoise-duty.lock"; flock -n 9 || { say "another breath is running; skip"; exit 0; }

STATUS_OUT=$("$PY" scripts/duty_status.py --json-only 2>>"$LOG"); CODE=$?
STATUS=$(echo "$STATUS_OUT" | "$PY" -c 'import sys,json; print(json.load(sys.stdin).get("snapshot",""))' 2>/dev/null)
case $CODE in
  0) ;;
  1) REASON=$(echo "$STATUS_OUT" | "$PY" -c 'import sys,json; print(json.load(sys.stdin)["exit_reason"])' 2>/dev/null)
     say "status=1 ${REASON:-idle}"; exit 0 ;;
  2) say "status=2 IBKR down; no dispatch (owner restarts and tells Hermes)"; exit 0 ;;
  3) REASON=$(echo "$STATUS_OUT" | "$PY" -c 'import sys,json; print(json.load(sys.stdin).get("exit_reason", "quota unavailable"))' 2>/dev/null)
     say "status=3 ${REASON:-quota unavailable}"; exit 0 ;;
  4) say "status=4 pool full or a dispatcher is active"; exit 0 ;;
  *) say "status exit $CODE (error)"; exit 0 ;;
esac

read -r HARNESS CAP EXE HOME_DIR < <("$PY" - "$STATUS" <<'EOF'
import json, sys
sys.path.insert(0, ".")
from workflows.harness_resolver import resolve_codex
s = json.load(open(sys.argv[1]))
h = resolve_codex(s["recommended_harness"])
print(h.name, s["capacity"]["launch_cap"], h.executable, h.codex_home)
EOF
)
say "status=0 wake $HARNESS launch_cap=$CAP status=$STATUS"

# Resolve concrete models once; fail closed if the selected identity's policy cannot be resolved.
DUTY_MODEL=$("$PY" -m workflows.codex_models --harness "$HARNESS" --role duty) || exit 1
DEBATE_MODEL=$("$PY" -m workflows.codex_models --harness "$HARNESS" --role debate) || exit 1
DUTY_MODEL_ARGS=(); DEBATE_MODEL_ARGS=()
[ -z "$DUTY_MODEL" ] || DUTY_MODEL_ARGS=(--model "$DUTY_MODEL")
[ -z "$DEBATE_MODEL" ] || DEBATE_MODEL_ARGS=(--model "$DEBATE_MODEL")
say "models duty=${DUTY_MODEL:-CLI-default} debate=${DEBATE_MODEL:-runner-default}"

# ── 2. 叫值班 Codex(短会话) ──
OUT="$ROOT/data/status/duty_wake_${TS}.json"
CODEX_HOME="$HOME_DIR" PATH="/root/.local/bin:$PATH" timeout 600 "$EXE" --ask-for-approval never \
  exec "${DUTY_MODEL_ARGS[@]}" --skip-git-repo-check -C "$ROOT" --sandbox workspace-write -o "$OUT" \
  "$(cat .codex/duty.md)

## Runtime Input
HARNESS: $HARNESS
STATUS: $STATUS" >>"$LOG" 2>&1 || say "codex exec exit $?"

DECISION=$(ls -t "$ROOT"/data/status/duty_decision_*.json 2>/dev/null | head -1)
if [ -z "$DECISION" ] || [ "$DECISION" -ot "$STATUS" ]; then say "no decision file written; nothing launched"; exit 0; fi

# ── 3. 核对(记 decision 行、更新轮空与推迟)并起单元 ──
"$PY" scripts/duty_record.py verify "$STATUS" "$DECISION" "$HARNESS" "$CAP" 2>>"$LOG" | while read -r MARKET KEY FLAG SYMS COST; do
  [ -z "$MARKET" ] && continue
  UNIT="equipoise-codex-duty-${MARKET}-${KEY}"
  if [ -n "${DUTY_DRY:-}" ]; then say "DRY: would launch $MARKET $SYMS as $UNIT ($HARNESS)"; continue; fi
  say "launch $MARKET $SYMS as $UNIT ($HARNESS)"
  systemd-run --collect --unit="$UNIT" --description="Equipoise $FLAG $KEY --harness $HARNESS" --property=WorkingDirectory="$ROOT" \
    --setenv=EQUIPOISE_IBKR_PORT=4001 \
    --setenv=PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    "$PY" "$ROOT/skills/equipoise-run-debates/scripts/codex_loop.py" \
    "$FLAG" "$KEY" --harness "$HARNESS" "${DEBATE_MODEL_ARGS[@]}" >>"$LOG" 2>&1 \
    && "$PY" scripts/duty_record.py launch "$TS" "$HARNESS" "$MARKET" "$SYMS" "$UNIT" "$KEY" "$COST" 2>>"$LOG" \
    || say "systemd-run failed for $UNIT"
done
systemctl reset-failed 'equipoise-codex-*' 2>/dev/null
say "breath done"
