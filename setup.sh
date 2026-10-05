#!/bin/bash
# douyu-highlight 一键部署脚本
# 用法: ./setup.sh
set -e

BASE="$(cd "$(dirname "$0")" && pwd)"
echo "=== douyu-highlight 部署 ==="

# 1. 检查依赖
echo "[1/6] 检查依赖..."
for cmd in python3 ffmpeg node npm; do
  if ! command -v $cmd &>/dev/null; then
    echo "缺少 $cmd，请先安装"
    exit 1
  fi
done
echo "依赖 OK"

# 2. 下载模型
echo "[2/6] 下载模型..."
mkdir -p ~/workspace/models/sense-voice
if [ ! -f ~/workspace/models/sense-voice/model.int8.onnx ]; then
  echo "下载 SenseVoice 模型 (229M)..."
  # hf-mirror 加速
  HF_ENDPOINT=https://hf-mirror.com huggingface-cli download \
    csukuangfj/sense-voice-onnx --include "model.int8.onnx" --include "tokens.txt" \
    --local-dir ~/workspace/models/sense-voice/ 2>/dev/null || \
  echo "请手动下载模型到 ~/workspace/models/sense-voice/"
fi
if [ ! -f ~/workspace/models/silero_vad.onnx ]; then
  echo "下载 Silero VAD 模型..."
  curl -sL -o ~/workspace/models/silero_vad.onnx \
    https://github.com/snakers4/silero-vad/raw/master/files/silero_vad.onnx || \
  echo "VAD 下载失败，转写将不跳静音"
fi

# 3. 建 Python venv
echo "[3/6] 创建转写环境..."
if [ ! -d ~/workspace/venvs/sherpa-onnx ]; then
  python3 -m venv ~/workspace/venvs/sherpa-onnx
  ~/workspace/venvs/sherpa-onnx/bin/pip install -q sherpa-onnx numpy
fi
echo "venv OK"

# 4. 装 biliLive-tools（录制器）
echo "[4/6] 安装 biliLive-tools..."
if [ ! -d ~/workspace/bililive-cli/node_modules ]; then
  cd ~/workspace/bililive-cli
  npm install --ignore-scripts bililive-cli@3.22.1 2>&1 | tail -1
  cd "$BASE"
fi
echo "biliLive-tools OK"

# 5. 初始化配置
echo "[5/6] 初始化配置..."
if [ ! -f ~/workspace/bililive-cli/highlight-rooms.json ]; then
  cp highlight-rooms.example.json ~/workspace/bililive-cli/highlight-rooms.json
  echo "已创建 highlight-rooms.json，请编辑填房间和密钥"
fi
mkdir -p ~/workspace/bililive-cli/queue/pending ~/workspace/bililive-cli/queue/done
mkdir -p ~/workspace/bililive-cli/highlights

# 6. 装 systemd 服务
echo "[6/6] 安装系统服务..."
sudo cp systemd/highlight-status.service /etc/systemd/system/
sudo cp systemd/highlight-pipeline.timer /etc/systemd/system/ 2>/dev/null || true
sudo systemctl daemon-reload
sudo systemctl enable --now highlight-status
echo ""
echo "=== 部署完成 ==="
echo "管理页: http://127.0.0.1:18022/ (默认密码 highlight)"
echo "下一步: 编辑 ~/workspace/bililive-cli/highlight-rooms.json 添加房间"
