#!/bin/bash
# highlight-pipeline 容器入口：每 20 分钟跑一轮
set -e

mkdir -p "$WORKDIR" "$(dirname "$STATE")" "$(dirname "$LOG")"

echo "[$(date '+%F %T')] highlight-pipeline 启动"
echo "  SEGDIR=$SEGDIR"
echo "  BILILIVE_API=$BILILIVE_API"

# 启动前等 biliLive API 就绪（最多 2 分钟）
# 用 passkey 做认证检查，避免 401 误报
for i in $(seq 1 24); do
  if [ -n "${BILILIVE_PASSKEY:-}" ] && curl -sf -o /dev/null \
      -H "Authorization: ${BILILIVE_PASSKEY}" \
      "${BILILIVE_API}/api/config" 2>/dev/null; then
    echo "[$(date '+%F %T')] biliLive API 就绪"
    break
  elif curl -sf -o /dev/null --max-time 3 \
      "${BILILIVE_API}/" 2>/dev/null; then
    # 无 passkey 时退化为 TCP 连通性检查
    echo "[$(date '+%F %T')] biliLive 端口可达（未验证认证）"
    break
  fi
  echo "[$(date '+%F %T')] 等待 biliLive API... ($i/24)"
  sleep 5
done

while true; do
  echo "[$(date '+%F %T')] 开始一轮"
  python3 /app/highlight-pipeline.py 2>&1 | tee -a "$LOG"
  echo "[$(date '+%F %T')] 本轮结束，20 分钟后继续"
  sleep 1200
done
