FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg（剪辑）+ 时区数据
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tzdata \
    curl \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# sherpa-onnx（SenseVoice 转写）
RUN pip install sherpa-onnx

# SenseVoice 模型（int8，229MB，随镜像走，运行时不用再下载）
# 从 hf-mirror 下载（国内快）；构建时如需用本地文件可改回 COPY
RUN mkdir -p /models/sense-voice && \
    cd /models/sense-voice && \
    curl -sL -o model.int8.onnx https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/model.int8.onnx && \
    curl -sL -o tokens.txt https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/tokens.txt && \
    ls -lh

# 流水线脚本
COPY highlight-pipeline.py llm_highlights.py highlight-status.py entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh

# 默认环境变量（docker-compose.yml 可覆盖）
ENV SEGDIR=/recordings \
    WORKDIR=/work/highlights \
    STATE=/work/highlight-state.json \
    LOG=/work/highlight.log \
    TRANSCRIBE_MODEL_DIR=/models/sense-voice \
    BILILIVE_API=http://bililive:18010 \
    STREAMER_NAME=兔了了丶 \
    DOUYU_ROOM_ID=6570336 \
    BILI_UID=500604364

WORKDIR /app
ENTRYPOINT ["/app/entrypoint.sh"]
