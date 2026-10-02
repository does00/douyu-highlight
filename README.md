# 斗鱼精彩片段流水线（Docker 版）

内容（SenseVoice 本地转写）+ 弹幕双信号，自动剪辑精彩片段并投稿到 B 站私密。

## 架构

| 容器 | 镜像 | 作用 |
|------|------|------|
| bililive | renmu1234/bililive-tools-backend | 录制斗鱼直播 + B站投稿 API（:18010）|
| highlight | ghcr.io/does00/douyu-highlight:latest | 转写→打分→剪辑→投稿，每 20 分钟一轮 |

## 部署（黑群晖 / 任意 x86_64 Docker）

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

1. 通过 biliLive-tools API 添加斗鱼房间（`POST http://NAS_IP:18010/recorder/add`，请求头带 PASSKEY）
2. 房间配置：ffmpeg 模式，开启自动录制
3. 在 B站账号管理里扫码登录（投稿用）

录像保存在 `./recordings/<主播>/`（.ts + .xml），highlight 容器会自动处理。

## 更新

```bash
cd douyu-highlight
git pull
docker compose pull highlight
docker compose up -d highlight
```

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
├── bililive-data/        # biliLive 配置（自动生成）
├── recordings/           # 录像文件（自动生成）
├── highlight-work/       # 转写缓存、state、日志（自动生成）
└── highlight/            # 流水线镜像源码
    ├── Dockerfile        # 模型构建时从 hf-mirror 下载
    ├── highlight-pipeline.py
    └── llm_highlights.py
```

## 说明

- 投稿均为"仅自己可见"，不会公开
- 完整录像保留在 `./recordings/`，只删已处理完的源文件
- LLM 标题功能默认关闭（需要中继）；如需开启，参考 docker-compose.yml 里的注释
