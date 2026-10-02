# 斗鱼精彩片段流水线（Docker 版）

内容（SenseVoice 本地转写）+ 弹幕双信号，自动剪辑精彩片段并投稿到 B 站私密。

## 架构

| 容器 | 镜像 | 作用 |
|------|------|------|
| bililive | renmu1234/bililive-tools-backend | 录制斗鱼直播 + B站投稿 API（:18010）|
| bililive-web | renmu1234/bililive-tools-frontend | Web 管理界面（:13000）|
| highlight | ghcr.io/does00/douyu-highlight:v1.0.0 | 转写→打分→剪辑→投稿，每 20 分钟一轮 |

## 工作原理

每 20 分钟一轮，每房间独立处理：

1. **扫描**：找新增的 `.ts` 录像（6 分钟无变动视为录完，已处理过的跳过）
2. **分桶**：按 30 分钟分桶，攒够 6 个文件或桶结束 40 分钟后合并处理
3. **转写**：sherpa-onnx SenseVoice 本地转写（词级时间戳，分块缓存可断点续跑）
4. **打分**：3 分钟滑动窗口双信号打分——语音侧（高能词/情绪标点/语速 spike/互动句式）+ 弹幕侧（高能词/独立用户/爆发加成）
5. **挑段**：LLM（DeepSeek/Gemini）挑段并起中文标题，与规则候选融合去重
6. **剪辑**：词边界吸附后 ffmpeg 剪辑（180–300 秒/条）
7. **投稿**：B 站私密投稿（仅自己可见），成功立即落盘去重

每房间独立 state 文件（`highlight-state-{room_id}.json`），投稿成功即记录，不会重复投稿。

## 部署（任意 x86_64 Docker）

```bash
# 1. 拉取本仓库
git clone https://github.com/does00/douyu-highlight.git
cd douyu-highlight

# 2. 复制 .env 并设置密钥
cp .env.example .env
# 编辑 .env，把 BILILIVE_PASSKEY 换成随机字符串

# 3. 拉预构建镜像并启动（无需本地编译）
docker compose pull
docker compose up -d

# 4. 看日志
docker compose logs -f highlight
```

## 首次配置

1. 打开 `http://NAS_IP:13000`
2. API 地址填 `http://NAS_IP:18010`，密钥填 `.env` 里的 `BILILIVE_PASSKEY`
3. 添加斗鱼房间（ffmpeg 模式），开启自动录制
4. 在 B站账号管理里扫码登录（投稿用）

录像保存在 `./recordings/<主播>/`（.ts + .xml），highlight 容器会自动处理。

## 多房间配置（可选）

默认是单房间模式（`SEGDIR`/`STREAMER_NAME`/`DOUYU_ROOM_ID` 环境变量）。要监控多个房间：

1. 复制 `highlight-rooms.example.json` 为 `highlight-rooms.json`，按格式填写房间和 AI 配置
2. 在 `docker-compose.yml` 的 highlight 服务里加：
   ```yaml
   volumes:
     - ./highlight-rooms.json:/work/highlight-rooms.json:ro
   environment:
     - ROOMS_CONFIG=/work/highlight-rooms.json
   ```
3. 重启 highlight 容器

房间配置示例见 `highlight-rooms.example.json`，关键字段：

| 字段 | 说明 |
|------|------|
| `room_id` / `streamer` / `segdir` | 房间号 / 主播名 / 录像目录 |
| `clip.keep_score` | 保留最低分（默认 10） |
| `clip.max_keep` | 每轮最多剪几条（默认 3） |
| `clip.min_clip` / `max_clip` | 片段时长范围秒（默认 180–300） |
| `clip.retention_days` | 录像保留天数，0=永久保留 |
| `season_id` | 投稿后自动加入的 B 站合集 ID（可选） |
| `ai.enabled` | 是否用 LLM 挑段起名 |

## AI 标题配置（可选）

在 `highlight-rooms.json` 的 `ai_global` 里配置（单房间模式下 LLM 关闭，只用时间标题）：

```json
"ai_global": {
  "provider": "deepseek",
  "model": "deepseek-chat",
  "api_key": "sk-...",
  "proxy": "http://proxy:port"
}
```

- `provider`：`deepseek` 或 `gemini`
- 有 `api_key` 则直连 API；也可以用 `relay_url` 走自建中继
- `proxy`：API 需要代理时填写

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SEGDIR` | `/recordings` | 录像目录（单房间模式） |
| `WORKDIR` | `/work/highlights` | 转写缓存、剪辑临时文件 |
| `BILILIVE_API` | `http://bililive:18010` | 投稿 API 地址 |
| `BILILIVE_PASSKEY` | — | API 密钥（必填） |
| `BILI_UID` | — | 投稿目标 B 站 UID |
| `DOUYU_ROOM_ID` / `STREAMER_NAME` | — | 单房间模式的房间号/主播名 |
| `ROOMS_CONFIG` | — | 多房间配置文件路径（不设则为单房间模式） |
| `RECORDING_RETENTION_DAYS` | `0` | 录像保留天数（单房间模式），0=永久保留 |
| `BILI_SESSDATA` | — | B 站 Cookie（可选，用于投稿后自动加入合集） |
| `TRANSCRIBE_MODEL_DIR` | `/models/sense-voice` | 转写模型目录（已内置于镜像） |

## 录像保留策略

- 每房间 `clip.retention_days`：处理完 N 天后删除本地 `.ts`/`.xml`，`0` 表示永久保留
- 完整录像**不上传**到 B 站，只传剪好的精彩片段
- 投稿均为"仅自己可见"，不会公开

## 更新

```bash
cd douyu-highlight
git pull
docker compose pull highlight
docker compose up -d highlight
```

锁定版本（不跟 latest）：把 `docker-compose.yml` 里 highlight 的 `image:` 改成 `ghcr.io/does00/douyu-highlight:v1.0.0`（版本号见 [Releases](../../releases)）。

## 本地构建（可选）

compose 默认拉 GHCR 预构建镜像。如需本地构建：
1. 注释掉 `docker-compose.yml` 里 highlight 的 `image:` 行
2. 取消 `build: ./highlight` 行的注释
3. `docker compose up -d --build`

## 目录说明

```
.
├── docker-compose.yml
├── .env                  # 密钥（自己建，不进 git）
├── highlight-rooms.json  # 多房间配置（可选，自己建，不进 git）
├── bililive-data/        # biliLive 配置（自动生成）
├── recordings/           # 录像文件（自动生成）
├── highlight-work/       # 转写缓存、state、日志（自动生成）
└── highlight/            # 流水线镜像源码
    ├── Dockerfile        # 模型构建时从 hf-mirror 下载
    ├── entrypoint.sh      # 每 20 分钟跑一轮
    ├── highlight-pipeline.py
    └── llm_highlights.py
```
