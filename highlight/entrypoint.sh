#!/bin/bash
# highlight-pipeline 容器入口：每 20 分钟跑一轮
set -e

mkdir -p "$WORKDIR" "$(dirname "$STATE")" "$(dirname "$LOG")"

echo "[$(date '+%F %T')] highlight-pipeline 启动"
echo "  SEGDIR=$SEGDIR"
echo "  BILILIVE_API=$BILILIVE_API"

# 启动前等 biliLive API 就绪（最多 2 分钟）
for i in $(seq 1 24); do
  if curl -sf -o /dev/null "${BILILIVE_API}/api/config" 2>/dev/null; then
    echo "[$(date '+%F %T')] biliLive API 就绪"
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
