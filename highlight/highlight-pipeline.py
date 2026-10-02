#!/usr/bin/env python3
"""
斗鱼精彩片段流水线 v2：内容（语音转写）+ 弹幕双信号。

流程：扫描已完成的 .ts 录像 → sherpa-onnx 本地转写（词级时间戳）
→ 3 分钟滑动窗口内容打分 + 弹幕加成 → 达标的窗口按词边界剪辑
→ B 站私密投稿。完整录像保留在本地。

v1 的问题已修：
- XML 没有 <video_start_time> 标签时不再整文件跳过（回退解析 p 属性首字段的相对秒）
- 冷房间纯弹幕检测颗粒无收：改内容信号做主力，弹幕只做加成

用法：
    python3 highlight-pipeline.py [--dry-run] [--include <ts文件名>] [--no-transcribe]

环境变量（沿用 v1，新增 TRANSCRIBE_MODEL_DIR）：
    SEGDIR / WORKDIR / STATE / LOG / BILILIVE_CONFIG / BILI_UID / DOUYU_ROOM_ID / STREAMER_NAME
"""
import json, os, re, subprocess, glob, statistics, sys, struct, wave, io
from datetime import datetime, timezone, timedelta

# ---------------- 配置 ----------------
LOCK = "/tmp/douyu-highlight.lock"
SEGDIR = os.environ.get("SEGDIR", "/home/hatch/Downloads/斗鱼/兔了了丶")
WORKDIR = os.environ.get("WORKDIR", "/home/hatch/workspace/bililive-cli/highlights")
STATE = os.environ.get("STATE", "/home/hatch/workspace/bililive-cli/highlight-state.json")
LOG = os.environ.get("LOG", "/home/hatch/workspace/bililive-cli/config/highlight.log")
BILILIVE_CONFIG = os.environ.get("BILILIVE_CONFIG", "/home/hatch/workspace/bililive-cli/config/appConfig.json")
BILILIVE_API = os.environ.get("BILILIVE_API", "http://127.0.0.1:18010")
BILILIVE_PASSKEY = os.environ.get("BILILIVE_PASSKEY", "")  # 设了就不用读 BILILIVE_CONFIG
UID = int(os.environ.get("BILI_UID", "500604364"))
ROOM_ID = os.environ.get("DOUYU_ROOM_ID", "6570336")
STREAMER = os.environ.get("STREAMER_NAME", "兔了了丶")
MODEL_DIR = os.environ.get("TRANSCRIBE_MODEL_DIR", "/home/hatch/workspace/models/sense-voice")

# 多房间配置（WebUI 管理）
ROOMS_CONFIG = os.environ.get("ROOMS_CONFIG",
    "/home/hatch/workspace/bililive-cli/highlight-rooms.json")
# 录像保留策略：0=永久保留（默认），N=处理完 N 天后删除 .ts/.xml
RETENTION_DAYS = int(os.environ.get("RECORDING_RETENTION_DAYS", "0"))

DRY_RUN = "--dry-run" in sys.argv
NO_TRANSCRIBE = "--no-transcribe" in sys.argv
ONLY_INCLUDE = None
if "--include" in sys.argv:
    ONLY_INCLUDE = sys.argv[sys.argv.index("--include") + 1]

TRANSCRIBE_CHUNK = 300.0   # 转写分块（秒）：每块单独抽音频
FEED_PIECE = 60.0          # 喂给 sherpa-onnx 的子块：600s 一次喂会静默退出，60s 已验证稳定
WIN_SEC = 180.0            # 评分滑动窗口
WIN_STEP = 60.0            # 滑动步长
DANMAKU_BIN = 30.0
KEEP_SCORE = 5.0           # 保留阈值（dry-run 校准用）
MIN_CLIP = 60.0
MAX_CLIP = 300.0

cst = timezone(timedelta(hours=8))

def log(msg):
    line = f"[{datetime.now(cst):%F %T}] {msg}"
    print(line, flush=True)
    if not DRY_RUN:
        with open(LOG, "a") as f:
            f.write(line + "\n")

# ---------------- 转写 ----------------
try:
    if NO_TRANSCRIBE:
        raise ImportError("no-transcribe flag")
    import sherpa_onnx
    _rec = None
    def _get_rec():
        global _rec
        if _rec is None:
            _rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=f"{MODEL_DIR}/model.int8.onnx",
                tokens=f"{MODEL_DIR}/tokens.txt",
                num_threads=2, language="zh", use_itn=True)
        return _rec
    TRANSCRIBE_OK = os.path.exists(f"{MODEL_DIR}/model.int8.onnx")
except ImportError:
    TRANSCRIBE_OK = False

if not TRANSCRIBE_OK:
    log("转写不可用（--no-transcribe 或模型缺失），降级为纯弹幕模式")

def extract_audio(ts_path, start, dur):
    """从 .ts 抽一段 16k 单声道 PCM，返回 bytes。"""
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(start), "-i", ts_path,
         "-t", str(dur), "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
         "-f", "wav", "pipe:1"],
        capture_output=True)
    if r.returncode != 0 or len(r.stdout) < 1000:
        return None
    bio = io.BytesIO(r.stdout)
    with wave.open(bio) as wf:
        n = wf.getnframes()
        raw = wf.readframes(n)
    return raw, n / 16000.0

def transcribe_piece(pcm_raw):
    """转写一段 PCM（<=60s），返回 [(t_start, token)]，t 相对段首。"""
    n = len(pcm_raw) // 2
    samples = [s / 32768.0 for s in struct.unpack(f"<{n}h", pcm_raw)]
    s = _get_rec().create_stream()
    s.accept_waveform(16000, samples)
    _get_rec().decode_stream(s)
    r = s.result
    return list(zip(r.timestamps, r.tokens)), r.text

# 当前处理房间（多房间循环时设置，用于转写缓存/去重命名空间隔离）
_CUR_ROOM_ID = None

def transcript_cache_path(ts_path, room_id=None):
    safe = re.sub(r"[^\w\-.]", "_", os.path.basename(ts_path))
    rid = room_id or _CUR_ROOM_ID
    if rid:
        safe = f"{rid}_{safe}"
    return os.path.join(WORKDIR, "transcripts", safe + ".json")

def load_cached_transcript(ts_path):
    """从分块缓存组装完整转写；缓存按 (size, mtime, chunk_idx) 键控，未完成可续跑。"""
    cp = transcript_cache_path(ts_path)
    try:
        d = json.load(open(cp))
        st = os.stat(ts_path)
        if d.get("size") == st.st_size and d.get("mtime") == st.st_mtime:
            chunks = {int(k): v for k, v in d.get("chunks", {}).items()}
            if chunks:
                log(f"  转写缓存命中 {len(chunks)} 块")
            return chunks
    except Exception:
        pass
    return {}

def save_chunk_cache(ts_path, chunks):
    try:
        cp = transcript_cache_path(ts_path)
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        st = os.stat(ts_path)
        tmp = cp + ".tmp"
        json.dump({"size": st.st_size, "mtime": st.st_mtime,
                   "chunks": {str(k): v for k, v in chunks.items()}},
                  open(tmp, "w"), ensure_ascii=False)
        os.replace(tmp, cp)  # 原子写入，避免被 kill 时损坏
    except Exception as e:
        log(f"  转写缓存写入失败: {e}")

def transcribe_chunk_words(ts_path, off, dur):
    """转写 [off, off+dur]（内部 60s 一喂），返回 [(全局秒, token)]。"""
    got = extract_audio(ts_path, off, dur)
    if not got:
        return []
    raw, _ = got
    import gc
    words = []
    pos = 0
    step = int(FEED_PIECE * 16000 * 2)
    while pos < len(raw):
        sub = raw[pos:pos + step]
        try:
            toks, _ = transcribe_piece(sub)
        except Exception as e:
            log(f"转写子块失败 @{off+pos/32000:.0f}s: {e}")
            pos += len(sub)
            continue
        base = off + pos / 32000.0
        for t, tok in toks:
            words.append((base + t, tok))
        pos += len(sub)
        del sub
    del raw
    gc.collect()
    return words

def transcribe_ts(ts_path):
    """转写整个 .ts（300s 一块），每块落盘缓存，被 kill 可续跑。返回 [(全局秒, token)]。"""
    dur = ts_duration(ts_path)
    chunks = load_cached_transcript(ts_path)
    off, idx, n_new = 0.0, 0, 0
    while off < dur:
        if idx not in chunks:
            cw = transcribe_chunk_words(ts_path, off, min(TRANSCRIBE_CHUNK, dur - off))
            chunks[idx] = cw
            n_new += 1
            save_chunk_cache(ts_path, chunks)  # 每块落盘，防 kill 丢进度
        off += TRANSCRIBE_CHUNK
        idx += 1
        if idx % 3 == 0:
            log(f"  转写进度 {off:.0f}/{dur:.0f}s")
    if n_new:
        log(f"  本次新转写 {n_new} 块")
    words = []
    for k in sorted(chunks):
        if k * TRANSCRIBE_CHUNK >= dur:
            continue  # 过滤超出当前时长的旧分块（文件被截短的边缘情况）
        words.extend(chunks[k])
    return words

def ts_duration(ts_path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", ts_path],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0

# ---------------- 弹幕解析（修 v1 bug） ----------------
def _merge_xml(ts_files, out_xml):
    """合并多个 .ts 对应的 .xml，弹幕时间按文件顺序偏移累加。"""
    import re
    all_items = []
    offset = 0.0
    for fp in ts_files:
        xp = fp[:-3] + ".xml"
        dur = ts_duration(fp)
        if not os.path.exists(xp):
            offset += dur
            continue
        try:
            txt = open(xp, encoding="utf-8", errors="ignore").read()
        except Exception:
            offset += dur
            continue
        # bilibili XML: p="时间,模式,..."
        def _shift(m):
            parts = m.group(1).split(",")
            try:
                parts[0] = f"{float(parts[0]) + offset:.2f}"
            except Exception:
                pass
            return 'p="' + ",".join(parts) + '"'
        txt = re.sub(r'p="([^"]+)"', _shift, txt)
        # 提取 <d> 标签
        for dm in re.finditer(r"<d p=.*?</d>", txt, re.S):
            all_items.append(dm.group(0))
        offset += dur
    try:
        with open(out_xml, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="utf-8"?>\n<i>\n')
            f.write("\n".join(all_items))
            f.write("\n</i>")
    except Exception as e:
        print(f"合并 XML 失败: {e}")

def parse_danmaku(xml_path):
    """返回 [(相对秒, user, uid, text)]。无 video_start_time 标签时回退解析 p 首字段。"""
    data = open(xml_path, encoding="utf-8", errors="ignore").read()
    m = re.search(r"<video_start_time>(\d+)</video_start_time>", data)
    rows = []
    if m:
        vst = int(m.group(1)) / 1000.0
        for t, user, uid, txt in re.findall(
                r'timestamp="(\d+)"[^>]*user="([^"]*)"[^>]*uid="(\d+)"[^>]*>([^<]*)</d>', data):
            rows.append((int(t) / 1000.0 - vst, user, uid, txt))
        # 兼容没有 user/uid 属性的写法
        if not rows:
            for t, txt in re.findall(r'timestamp="(\d+)"[^>]*>([^<]*)</d>', data):
                rows.append((int(t) / 1000.0 - vst, "", "", txt))
    else:
        # 回退：p 属性首字段就是相对秒（已验证 timestamp-p 反推一致）
        for p, user, uid, txt in re.findall(
                r'<d p="([\d.]+),[^"]*"[^>]*user="([^"]*)"[^>]*uid="(\d+)"[^>]*>([^<]*)</d>', data):
            rows.append((float(p), user, uid, txt))
        if not rows:
            for p, txt in re.findall(r'<d p="([\d.]+),[^>]*>([^<]*)</d>', data):
                rows.append((float(p), "", "", txt))
    rows.sort()
    return rows

def danmaku_bursts(danmaku):
    """30s 分箱，阈值 max(2, 2×中位数)（冷房间调低，验证层会过滤）。返回爆发箱索引 set。"""
    if not danmaku:
        return set(), 0
    bins = {}
    for t, _, _, _ in danmaku:
        b = int(t // DANMAKU_BIN)
        bins[b] = bins.get(b, 0) + 1
    med = statistics.median(bins.values())
    thr = max(2, 2 * med)
    return {b for b, c in bins.items() if c >= thr}, thr

# ---------------- 内容打分（Tier 1 规则） ----------------
HYPE_RE = re.compile(r"哈哈|大笑|笑死|卧槽|我操|我靠|牛逼|牛比|好家伙|漂亮|天呐|我的天|绝了|神了|离谱|救命|刺激|过瘾|精彩|666|awsl|泪目|绷不住")
INTERACT_RE = re.compile(r"你们|兄弟们|宝子|家人们|宝宝们|看好了|注意看|来了来了|全体起立")
DM_HYPE_RE = re.compile(r"哈哈|233|666|hhh|卧槽|awsl|牛[逼比bB]|离谱|绷不住|笑死|泪目|[！!]{3,}")

def score_window(ws, we, words, danmaku, bursts):
    """对 [ws, we] 窗口打分，返回 (score, reasons)。"""
    text = "".join(tok for t, tok in words if ws <= t <= we)
    wts = [t for t, tok in words if ws <= t <= we]
    score, reasons = 0.0, []

    # --- 内容信号 ---
    hype_n = len(HYPE_RE.findall(text))
    if hype_n:
        s = min(10, 2 * hype_n); score += s; reasons.append(f"高能词×{hype_n}+{s}")
    punct_n = text.count("！") + text.count("？")
    if punct_n:
        s = min(5, punct_n); score += s; reasons.append(f"情绪标点×{punct_n}+{s}")
    inter_n = len(INTERACT_RE.findall(text))
    if inter_n:
        s = min(3, inter_n); score += s; reasons.append(f"互动句式×{inter_n}+{s}")
    # 语速 spike：最热 60s 字数 vs 窗口平均
    if len(text) >= 60:
        best = 0
        for b in range(0, int(we - ws) - 60 + 1, 10):
            c = sum(1 for t in wts if ws + b <= t <= ws + b + 60)
            best = max(best, c)
        avg = len(text) / ((we - ws) / 60.0)
        if avg > 0 and best / avg >= 1.6 and best >= 40:
            score += 3; reasons.append(f"语速spike{best/avg:.1f}x+3")
    # 沉默惩罚：3 分钟说不到 40 个字 ≈ 挂机
    if len(text) < 40:
        score -= 6; reasons.append("沉默-6")
    # 念弹幕降权：弹幕原文（≥4字）出现在转写里 → 主播在念弹幕
    read_n = 0
    for t, _, _, dtxt in danmaku:
        if ws - 20 <= t <= we and len(dtxt) >= 4 and dtxt in text:
            read_n += 1
    if read_n:
        s = min(12, 4 * read_n); score -= s; reasons.append(f"念弹幕×{read_n}-{s}")

    # --- 观众信号（加成） ---
    dm_in = [(t, u, i, x) for t, u, i, x in danmaku if ws - 20 <= t <= we + 5]
    dhype = sum(len(DM_HYPE_RE.findall(x)) for _, _, _, x in dm_in)
    if dhype:
        s = min(6, 2 * dhype); score += s; reasons.append(f"弹幕高能×{dhype}+{s}")
    users = {i for _, _, i, _ in dm_in if i}
    if users:
        s = min(4, len(users)); score += s; reasons.append(f"弹幕{len(users)}人+{s}")
    # 反刷屏：单用户单窗已在 users 去重里自然处理
    burst_hit = any(int((ws - 20) // DANMAKU_BIN) <= b <= int((we + 5) // DANMAKU_BIN) for b in bursts)
    if burst_hit:
        score += 3; reasons.append("弹幕爆发+3")

    return score, reasons, text[:60]

def nms(cands, max_keep=6):
    """非极大值抑制：按分数降序贪心取不重叠窗口。
    先按分数取 TopN，再按时间排序。cands 元素为 (ws,we,score,reasons,title)。"""
    cands = sorted(cands, key=lambda x: -x[2])
    kept = []
    for c in cands:
        ws, we = c[0], c[1]
        if all(we <= k[0] or ws >= k[1] for k in kept):
            kept.append(c)
    # 先按分数截断，再按时间排序（保证取的是最高分）
    kept = sorted(kept, key=lambda x: -x[2])[:max_keep]
    kept.sort()
    return kept

def find_candidates(words, danmaku, max_keep=6):
    """滑动窗口打分 + 非极大值抑制，返回 [(ws, we, score, reasons, title)]（按时间排序）。"""
    if not words:
        return []
    bursts, thr = danmaku_bursts(danmaku)
    dur = words[-1][0]
    cands = []
    ws = 0.0
    while ws + WIN_SEC <= dur + WIN_STEP:
        we = min(ws + WIN_SEC, dur)
        score, reasons, _ = score_window(ws, we, words, danmaku, bursts)
        if score >= KEEP_SCORE:
            cands.append((ws, we, score, reasons, None))
        ws += WIN_STEP
    return nms(cands, max_keep)

def snap_boundaries(ws, we, words):
    """按词边界吸附：开头取 ws-2s 后的第一个词起（-0.5s），结尾取 we+2s 前的最后一个词止（+0.5s）。"""
    cs, ce = ws, we
    for t, _ in words:
        if t >= ws - 2:
            cs = max(0.0, t - 0.5); break
    for t, _ in reversed(words):
        if t <= we + 2:
            ce = t + 0.5; break
    if ce - cs < MIN_CLIP:
        ce = min(cs + MIN_CLIP, we + 30)
    if ce - cs > MAX_CLIP:
        ce = cs + MAX_CLIP
    return cs, ce

# ---------------- 剪辑与投稿（沿用 v1） ----------------
def clip(ts_path, cs, ce, out_path):
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(cs), "-i", ts_path,
                        "-t", str(ce - cs), "-c:v", "libx264", "-crf", "23",
                        "-preset", "veryfast", "-c:a", "aac", out_path])
    return r.returncode == 0 and os.path.exists(out_path)

def _bili_season_headers():
    """B 站创作中心 API 请求头（需要 SESSDATA）。"""
    import os
    sessdata = os.environ.get("BILI_SESSDATA", "").strip()
    if not sessdata:
        # 从房间配置的全局设置读
        try:
            cfg = json.load(open(ROOMS_CONFIG))
            sessdata = cfg.get("bili_sessdata", "").strip()
        except Exception:
            pass
    if not sessdata:
        return None
    # 从 cookie 提取 bili_jct 做 csrf
    import re
    m = re.search(r"bili_jct=([^;]+)", sessdata)
    csrf = m.group(1) if m else ""
    if not csrf:
        log("  加合集跳过：Cookie 缺少 bili_jct，请在 AI 配置页填写完整 Cookie（含 bili_jct）")
        return""
    # sessdata 可能是不带键名的纯值
    cookie = sessdata if "SESSDATA=" in sessdata else f"SESSDATA={sessdata}"
    if csrf and "bili_jct=" not in cookie:
        cookie += f"; bili_jct={csrf}"
    return {
        "Cookie": cookie,
        "csrf": csrf,
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Referer": "https://member.bilibili.com/",
        "Origin": "https://member.bilibili.com",
    }


def _get_season_section_id(season_id, headers):
    """获取合集的默认 section_id。"""
    import urllib.request
    url = (f"https://member.bilibili.com/x2/creative/web/season/sections"
           f"?season_id={season_id}")
    req = urllib.request.Request(url, headers={k: v for k, v in headers.items() if k != "csrf"})
    with urllib.request.urlopen(req, timeout=15) as r:
        j = json.load(r)
    if j.get("code") != 0:
        raise RuntimeError(f"获取 section 失败: {j.get('message')}")
    sections = j["data"]["sections"]
    if not sections:
        raise RuntimeError("合集下无分区")
    return sections[0]["id"]


def _add_video_to_season(aid, title, season_id, headers):
    """把视频加入合集。需要 aid（投稿完成后获取）。"""
    import urllib.request
    # 先拿 cid（视频详情）
    url = f"https://api.bilibili.com/x/web-interface/view?aid={aid}"
    req = urllib.request.Request(url, headers={k: v for k, v in headers.items() if k != "csrf"})
    with urllib.request.urlopen(req, timeout=15) as r:
        j = json.load(r)
    if j.get("code") != 0:
        raise RuntimeError(f"获取视频信息失败: {j.get('message')}")
    cid = j["data"]["cid"]
    # 加到合集
    section_id = _get_season_section_id(season_id, headers)
    payload = {
        "section_id": section_id,
        "episode": [{"aid": aid, "cid": cid, "title": title, "charging_pay": 0}],
    }
    data = json.dumps(payload).encode()
    url = (f"https://member.bilibili.com/x2/creative/web/season/section/"
           f"episodes/add?csrf={headers['csrf']}")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "Cookie": headers["Cookie"]})
    with urllib.request.urlopen(req, timeout=15) as r:
        j = json.load(r)
    if j.get("code") != 0:
        raise RuntimeError(f"加入合集失败: {j.get('message')}")
    return True


def _find_aid_by_title(title, headers, retries=12):
    """按标题在用户投稿列表里找 aid（投稿转码完成后出现）。"""
    import urllib.request
    import urllib.parse
    import time
    for i in range(retries):
        url = (f"https://api.bilibili.com/x/space/arc/search?mid={UID}"
               f"&ps=10&pn=1&order=pubdate")
        req = urllib.request.Request(url, headers={k: v for k, v in headers.items() if k != "csrf"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                j = json.load(r)
            for v in j.get("data", {}).get("list", {}).get("vlist", []):
                if v.get("title", "").strip() == title.strip():
                    return v["aid"]
        except Exception:
            pass
        if i < retries - 1:
            time.sleep(30)  # 等 30 秒再查
    return None


def _add_to_season_after_upload(title, season_id, log=print):
    """投稿后自动加入合集（后台执行，不阻塞主流程）。"""
    headers = _bili_season_headers()
    if not headers:
        log("  未配置 BILI_SESSDATA，跳过加合集")
        return
    if not season_id:
        return
    try:
        aid = _find_aid_by_title(title, headers)
        if not aid:
            log(f"  加合集失败：找不到视频 {title[:20]}")
            return
        _add_video_to_season(aid, title, season_id, headers)
        log(f"  已加入合集 season_id={season_id} aid={aid}")
    except Exception as e:
        log(f"  加合集异常: {type(e).__name__}: {e}")


def upload(path, title, desc=None):
    import urllib.request
    passkey = BILILIVE_PASSKEY or json.load(open(BILILIVE_CONFIG))["passKey"]
    payload = {"uid": UID, "videos": [path], "config": {
        "title": title,
        "desc": desc or f"斗鱼房间 {ROOM_ID} 直播精彩片段（内容+弹幕双信号自动剪辑，仅自己可见）",
        "tag": ["精彩片段", "录播", "游戏直播"],
        "tid": 5, "copyright": 1,
        "source": f"https://www.douyu.com/{ROOM_ID}",
        "dolby": 0, "hires": 0, "is_only_self": 1,
        "noReprint": 0, "closeDanmu": 0, "closeReply": 0, "no_disturbance": 1},
        "options": {"removeOriginAfterUploadCheck": True}}
    req = urllib.request.Request(f"{BILILIVE_API}/bili/upload",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": passkey})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["taskId"]

# ---------------- AstrBot QQ 通知 ----------------
# 投稿成功后经 AstrBot OpenAPI 发 QQ 通知。配置：环境变量优先，
# 否则读 highlight-rooms.json 的 notify 节（管理页 AI 配置 Tab 维护）。
# fail-open：失败只记日志，不影响流水线。
def _notify_cfg():
    cfg = {}
    try:
        cfg = json.load(open(ROOMS_CONFIG)).get("notify", {}) or {}
    except Exception:
        pass
    url = os.environ.get("ASTRBOT_NOTIFY_URL", "") or cfg.get("url", "")
    return {
        "url": url.rstrip("/"),
        "key": os.environ.get("ASTRBOT_NOTIFY_KEY", "") or cfg.get("api_key", ""),
        "umo": os.environ.get("ASTRBOT_NOTIFY_UMO", "") or cfg.get("umo", ""),
        "enabled": bool(cfg.get("enabled", True)),
    }

def _egress_proxy():
    """沙箱出站直连会被透明劫持 RST：取 CONNECT 代理地址。"""
    p = (os.environ.get("PROXY_URL") or os.environ.get("HTTPS_PROXY")
         or os.environ.get("HTTP_PROXY") or "")
    if not p:
        try:
            with open("/home/hatch/agsbx/.proxyenv") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("EGRESS_PROXY="):
                        p = line.split("=", 1)[1].strip()
                        break
        except Exception:
            pass
    return p

def _urlopen(req, timeout):
    """经 egress CONNECT 代理请求（沙箱直连会被 RST）；无代理配置时直连。"""
    import urllib.request
    proxy = _egress_proxy()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)

def notify_qq_upload(title, duration_s, streamer=None):
    n = _notify_cfg()
    if not (n["enabled"] and n["url"] and n["key"] and n["umo"]):
        return
    import urllib.request
    text = (f"\U0001f4e4 B站投稿成功\n"
            f"主播：{streamer or STREAMER}\n"
            f"标题：{title}\n"
            f"时长：{duration_s:.0f}s")
    payload = {"umo": n["umo"], "message": text}
    try:
        req = urllib.request.Request(
            f"{n['url']}/api/v1/im/messages",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "X-API-Key": n["key"]})
        with _urlopen(req, timeout=30) as r:
            log(f"  QQ通知已发送: {r.status}")
    except Exception as e:
        log(f"  QQ通知失败: {e}")

# ---------------- 主流程 ----------------
def load_rooms_config():
    """读取多房间配置。没有配置文件时回退到单房间（环境变量）模式。"""
    try:
        cfg = json.load(open(ROOMS_CONFIG))
        rooms = [r for r in cfg.get("rooms", []) if r.get("enabled", True)]
        if rooms:
            return rooms
    except Exception as e:
        log(f"房间配置读取失败 {ROOMS_CONFIG}: {e}")
    # 回退：单房间（环境变量）
    return [{
        "room_id": ROOM_ID, "streamer": STREAMER,
        "segdir": SEGDIR, "enabled": True,
        "clip": {}, "ai": {"enabled": True},
    }]


def _configure_llm(room):
    """从房间配置设置 llm_highlights（provider/model/key/proxy）。"""
    import llm_highlights as L
    # 全局 AI 配置（highlight-rooms.json 的 ai_global）
    try:
        cfg = json.load(open(ROOMS_CONFIG))
        g = cfg.get("ai_global", {})
    except Exception:
        g = {}
    # 房间 AI 开关已在外层判断，这里只同步连接参数
    # 先重置，避免多房间顺序处理时残留上一个房间的状态
    L.LLM_OK = False
    L.USE_DIRECT = False
    pv = g.get("provider", "deepseek")
    L.LLM_PROVIDER = pv
    # 模型按 provider 校验（防止存的是别的 provider 的模型名）
    valid_ds = ("deepseek-chat", "deepseek-flash", "deepseek-v4-pro",
                "deepseek-reasoner")
    valid_gm = ("gemini-3-flash-preview", "gemini-2.0-flash",
                "gemini-1.5-flash", "gemini-1.5-pro")
    m = g.get("model", "")
    if pv == "deepseek":
        L.DEEPSEEK_MODEL = m if m in valid_ds else "deepseek-chat"
    elif pv == "gemini":
        L.GEMINI_MODEL = m if m in valid_gm else "gemini-3-flash-preview"
    if g.get("proxy"):
        L.HTTP_PROXY = g["proxy"]
    if g.get("api_key"):
        if pv == "deepseek":
            L.DEEPSEEK_API_KEY = g["api_key"]
        elif pv == "gemini":
            L.GEMINI_API_KEY = g["api_key"]
        L.LLM_OK = True
        L.USE_DIRECT = True
    L.LLM_MODEL = (L.DEEPSEEK_MODEL if pv == "deepseek"
                   else L.GEMINI_MODEL if pv == "gemini"
                   else "gemini-3-flash-preview")
    # 同步房间的时长限制
    L.LLM_MIN_CLIP = float(MIN_CLIP)
    L.LLM_MAX_CLIP = float(MAX_CLIP)
    return L


def _build_desc(reasons):
    """从选中原因生成观众看得懂的简介。
    LLM 的 why 直接用；规则版的技术指标转人话。"""
    if not reasons:
        return None
    # 过滤掉内部标记，只留人话
    skip = {"LLM-pick", "danmaku+1", "纯弹幕降级"}
    human = [r for r in reasons if r not in skip]
    # LLM 的 why 通常是完整中文句子（不含技术格式），直接用
    for r in human:
        # 技术指标格式：含 ×，或 x 后跟数字/+（如 "高能词×1+2", "语速spike1.8x+3"）
        if "×" in r or re.search(r"x[+\d]", r):
            continue
        if len(r) > 10:
            return r[:100]
    # 规则版技术指标转人话
    parts = []
    for r in human:
        if "高能词" in r:
            parts.append("高能时刻")
        elif "情绪标点" in r:
            parts.append("情绪爆发")
        elif "语速spike" in r:
            parts.append("语速飙升")
        elif "互动句式" in r:
            parts.append("观众互动")
        elif "弹幕爆发" in r or "弹幕高能" in r:
            parts.append("弹幕刷屏")
        elif "念弹幕" in r:
            continue  # 降权项不展示
    if parts:
        # 去重保持顺序
        seen, uniq = set(), []
        for p in parts:
            if p not in seen:
                seen.add(p); uniq.append(p)
        return "看点：" + "、".join(uniq[:4])
    return None


def process_room(room):
    """处理单个房间。"""
    global SEGDIR, ROOM_ID, STREAMER, STATE, WIN_SEC, WIN_STEP
    global KEEP_SCORE, RETENTION_DAYS, _CUR_ROOM_ID
    global MIN_CLIP, MAX_CLIP

    rid = str(room.get("room_id", ""))
    _CUR_ROOM_ID = rid
    SEGDIR = room.get("segdir") or SEGDIR
    ROOM_ID = rid
    STREAMER = room.get("streamer") or STREAMER
    # 每房间独立 state 文件（多房间模式始终按房间隔离，不读 STATE 环境变量）
    STATE = os.path.join(os.path.dirname(WORKDIR), f"highlight-state-{rid}.json")
    # 每房间剪辑参数
    clip_cfg = room.get("clip", {}) or {}
    WIN_SEC = float(clip_cfg.get("win_sec", 180))
    WIN_STEP = float(clip_cfg.get("win_step", 60))
    KEEP_SCORE = float(clip_cfg.get("keep_score", 10))
    MAX_KEEP = int(clip_cfg.get("max_keep", 3))
    MIN_CLIP = float(clip_cfg.get("min_clip", 180))
    MAX_CLIP = float(clip_cfg.get("max_clip", 300))
    RETENTION_DAYS = int(clip_cfg.get("retention_days",
        int(os.environ.get("RECORDING_RETENTION_DAYS", "0"))))
    AI_ENABLED = (room.get("ai", {}) or {}).get("enabled", True)

    log(f"===== 房间 {rid}（{STREAMER}） =====")
    log(f"  目录: {SEGDIR}")
    log(f"  参数: win={WIN_SEC:g}s step={WIN_STEP:g}s score>={KEEP_SCORE:g} "
        f"max={MAX_KEEP} clip={MIN_CLIP:g}-{MAX_CLIP:g}s "
        f"retention={RETENTION_DAYS}d ai={'开' if AI_ENABLED else '关'}")

    os.makedirs(WORKDIR, exist_ok=True)
    if os.path.exists(STATE):
        state = json.load(open(STATE))
    else:
        state = {"processed": [], "uploaded": []}
    processed = set(state.get("processed", []))
    # 处理时间戳（用于保留策略清理）
    processed_at = state.get("processed_at", {})
    # 已投稿候选 ID 集合（用于幂等去重）
    uploaded_ids = {u.get("cid") for u in state.get("uploaded", []) if u.get("cid")}

    def save_state():
        """立即持久化 state（投稿成功后调用，防崩溃重复投稿）。"""
        if not DRY_RUN:
            state["processed"] = sorted(processed)
            state["processed_at"] = processed_at
            json.dump(state, open(STATE, "w"))

    # 保留策略清理：删除处理完超过 N 天的 .ts/.xml
    # 成品片段 .mp4 共用同一个保留天数参数
    if RETENTION_DAYS > 0 and not DRY_RUN:
        import time
        cutoff = time.time() - RETENTION_DAYS * 86400
        for _mp4 in glob.glob(os.path.join(WORKDIR, "*_精彩_*.mp4")):
            try:
                if os.path.getmtime(_mp4) < cutoff:
                    os.remove(_mp4)
                    log(f"  成品保留期满删除: {os.path.basename(_mp4)}")
            except OSError:
                pass
        for base, ts in list(processed_at.items()):
            if ts < cutoff:
                ts_path = os.path.join(SEGDIR, base)
                xml_path = os.path.splitext(ts_path)[0] + ".xml"
                for p in (ts_path, xml_path):
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                            log(f"  保留期满删除: {os.path.basename(p)}")
                        except OSError as e:
                            log(f"  删除失败 {p}: {e}")
                # 从记录中移除（避免重复尝试）
                processed.discard(base)
                processed_at.pop(base, None)
        save_state()

    # 找已完成（6 分钟无变动）的 .ts
    now = datetime.now().timestamp()
    tss = []
    for p in sorted(glob.glob(os.path.join(SEGDIR, "*.ts"))):
        base = os.path.basename(p)
        if ONLY_INCLUDE and base != ONLY_INCLUDE:
            continue
        if base in processed:
            continue
        if "15-00-05-840" in base:  # v1 遗留：旧会话重复文件
            processed.add(base); continue
        if os.path.getmtime(p) < now - 360:  # 文件6分钟无变动视为写完
            tss.append(p)
    if not tss:
        log("no new ts, exit")
        if not DRY_RUN:
            state["processed"] = sorted(processed)
            json.dump(state, open(STATE, "w"))
        return

    # 按 30 分钟窗口分组，合并连续的 5 分钟文件
    from collections import defaultdict
    def _ts_time(fn):
        import re
        m = re.search(r"(\d{4}-\d{2}-\d{2}) (\d{2})-(\d{2})-(\d{2})", fn)
        if not m:
            return None
        from datetime import datetime
        return datetime.strptime(f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}",
                                 "%Y-%m-%d %H:%M:%S")
    buckets = defaultdict(list)
    for p in tss:
        tm = _ts_time(os.path.basename(p))
        if not tm:
            buckets["nogroup"].append(p)
            continue
        # 按 30 分钟向下取整
        bucket = tm.replace(minute=(tm.minute // 30) * 30, second=0, microsecond=0)
        buckets[bucket].append(p)
    # 只处理"完整"的桶：满 6 个文件，或桶时间已过去 40 分钟（不会再有新文件）
    merged_tasks = []  # [(merged_ts, [original_paths])]
    for bucket, files in sorted(buckets.items()):
        if bucket == "nogroup":
            for p in files:
                merged_tasks.append((p, [p]))
            continue
        files = sorted(files)
        bucket_end = bucket.timestamp() + 1800
        is_complete = len(files) >= 6 or now > bucket_end + 2400
        if not is_complete:
            log(f" 桶 {bucket.strftime('%H:%M')} 只有 {len(files)} 个文件，等待更多")
            continue  # 不标记为已处理，下次再来
        if len(files) == 1:
            merged_tasks.append((files[0], files))
        else:
            # ffmpeg 合并
            merged = os.path.join(WORKDIR, f"merged_{bucket.strftime('%Y%m%d_%H%M')}.ts")
            if not os.path.exists(merged):
                flist = os.path.join(WORKDIR, "merge_list.txt")
                with open(flist, "w") as f:
                    for fp in files:
                        f.write(f"file '{fp}'\n")
                r = subprocess.run(
                    ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                     "-i", flist, "-c", "copy", merged],
                    capture_output=True, timeout=300)
                if r.returncode != 0 or not os.path.exists(merged):
                    log(f"  合并失败，逐个处理")
                    for p in files:
                        merged_tasks.append((p, [p]))
                    continue
                log(f"  合并 {len(files)} 个文件 -> {os.path.basename(merged)}")
            # 合并弹幕 XML（时间偏移累加）
            _merge_xml(files, merged[:-3] + ".xml")
            merged_tasks.append((merged, files))

    for ts_path, orig_files in merged_tasks:
        base = os.path.basename(ts_path)
        log(f"处理 {base[:30]} ({ts_duration(ts_path):.0f}s)")
        xml_path = ts_path[:-3] + ".xml"
        danmaku = parse_danmaku(xml_path) if os.path.exists(xml_path) else []
        log(f"  弹幕 {len(danmaku)} 条")

        if TRANSCRIBE_OK:
            log("  转写中...")
            words = transcribe_ts(ts_path)  # 内部分块缓存，未完成可续跑
            log(f"  转写完成 {len(words)} 词")
        else:
            words = []

        if words:
            cands = find_candidates(words, danmaku, max_keep=MAX_KEEP)
            # Phase 2: LLM 挑段（失败则跳过，用规则候选兜底）
            if "--no-llm" not in sys.argv and AI_ENABLED:
                try:
                    L = _configure_llm(room)
                    from llm_highlights import pick_highlights_llm
                    if L.LLM_OK:
                        log(f"  LLM 挑选中 [{L.LLM_PROVIDER}/{L.LLM_MODEL}]...")
                        llm_cands = pick_highlights_llm(words, danmaku, log=log)
                        log(f"  LLM 候选 {len(llm_cands)} 个")
                        cands = nms(cands + llm_cands, max_keep=MAX_KEEP)  # 融合去重（LLM 基分 20，优先）
                    else:
                        log("  LLM 不可用，跳过")
                except Exception as e:
                    log(f"  LLM 阶段异常，跳过: {e}")
        else:
            # 降级：纯弹幕模式（v1 逻辑简化版）
            bursts, _ = danmaku_bursts(danmaku)
            raw = []
            for b in sorted(bursts):
                ws = max(0, b * DANMAKU_BIN - 90)
                raw.append((ws, ws + 180, 0, ["纯弹幕降级"], None))
            cands = []
            for ws, we, sc, rs, tt in raw:
                if cands and ws <= cands[-1][1] + 30:
                    pws, pwe, psc, prs, ptt = cands[-1]
                    cands[-1] = (pws, max(pwe, we), psc, prs, ptt)
                else:
                    cands.append((ws, we, sc, rs, tt))
        log(f"  候选 {len(cands)} 个")
        for ws, we, sc, rs, tt in cands:
            log(f"    [{ws:.0f},{we:.0f}] 分={sc:g} {'|'.join(rs)}" + (f" 标题:{tt}" if tt else ""))

        if DRY_RUN:
            continue

        # 从录像文件名解析开播时间（"2026-09-30 12-05-17-265 ..."），用于片段标题
        m = re.match(r"(\d{4}-\d{2}-\d{2}) (\d{2})-(\d{2})-(\d{2})", base)
        rec_start = None
        if m:
            try:
                rec_start = datetime.strptime(
                    f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}",
                    "%Y-%m-%d %H:%M:%S").replace(tzinfo=cst)
            except ValueError:
                pass

        for ws, we, sc, rs, tt in cands:
            cs, ce = snap_boundaries(ws, we, words) if words else (ws, we)
            # 稳定候选 ID：源文件 size+mtime + 片段起止（幂等去重）
            st = os.stat(ts_path)
            cid = f"{rid}_{st.st_size}_{int(st.st_mtime)}_{cs:.0f}_{ce:.0f}"
            if cid in uploaded_ids:
                log(f"  跳过已投稿 [{cs:.0f},{ce:.0f}]")
                continue
            # 简介：从选中原因提取观众看点（LLM 的 why / 规则版转人话）
            desc = _build_desc(rs)
            # 加上直播间链接
            room_url = f"https://www.douyu.com/{ROOM_ID}"
            desc = (desc + "\n" + room_url) if desc else room_url
            if rec_start:
                clip_t = rec_start + timedelta(seconds=cs)
            else:
                clip_t = datetime.now(cst)
            tstr = clip_t.strftime("%H%M%S")
            day = clip_t.strftime("%m-%d")
            out = os.path.join(WORKDIR, f"{STREAMER}_{day}_精彩_{tstr}.mp4")
            if not clip(ts_path, cs, ce, out):
                log(f"  剪辑失败 [{cs:.0f},{ce:.0f}]"); continue
            # 标题：候选自带的 LLM 标题优先，否则再生成，失败回退时间标题
            # 简介：已在循环开头从选中原因生成（_build_desc）
            title = tt  # 挑段阶段 LLM 给的标题
            if not title and "--no-llm" not in sys.argv and AI_ENABLED and words:
                try:
                    _configure_llm(room)  # 确保 LLM 已配置（挑段阶段可能跳过）
                    from llm_highlights import generate_title_and_desc
                    excerpt = "".join(tok for t, tok in words if ws <= t <= we)
                    title, _ = generate_title_and_desc(excerpt, log=log)
                except Exception as e:
                    log(f"  标题生成失败: {e}")
            if title:
                title = f"[{STREAMER}] {title} {day}"
            else:
                title = f"[{STREAMER}] 直播精彩片段 {day} {tstr}"
            try:
                task = upload(out, title, desc)
                log(f"  已投稿: {title} ({ce-cs:.0f}s) task={task}")
                notify_qq_upload(title, ce - cs)
                state.setdefault("uploaded", []).append(
                    {"cid": cid, "title": title, "desc": desc,
                     "start": cs, "end": ce, "score": sc,
                     "reasons": rs, "task": task})
                uploaded_ids.add(cid)
                save_state()  # 立即保存，防崩溃重复投稿
                # 自动加入合集（同步，确保完成）
                season_id = room.get("season_id")
                if season_id:
                    try:
                        _add_to_season_after_upload(title, season_id, log)
                    except Exception as e:
                        log(f"  加合集失败: {e}")
            except Exception as e:
                log(f"  投稿失败 {title}: {e}")

        for _fp in orig_files:
            processed.add(os.path.basename(_fp))
        import time as _time
        processed_at[base] = _time.time()
        # 录像保留策略由 RECORDING_RETENTION_DAYS 控制（0=永久保留）
        # 清理逻辑在 main() 开头统一执行
        save_state()
        # 删临时合并文件（merged_*.ts/xml），源文件按保留策略另行处理
        if os.path.basename(ts_path).startswith("merged_") and os.path.dirname(ts_path) == WORKDIR:
            for _mp in (ts_path, ts_path[:-3] + ".xml"):
                try:
                    if os.path.exists(_mp):
                        os.remove(_mp)
                except OSError:
                    pass

    log("done")


def main():
    """多房间循环：逐个处理启用的房间，单个房间失败不阻塞其他。"""
    rooms = load_rooms_config()
    log(f"共 {len(rooms)} 个启用房间")
    for room in rooms:
        try:
            process_room(room)
        except Exception as e:
            rid = room.get("room_id", "?")
            log(f"房间 {rid} 处理异常，跳过: {type(e).__name__}: {e}")
            import traceback
            log(traceback.format_exc()[-500:])
    log("全部房间处理完成")

if __name__ == "__main__":
    # 单实例锁（沿用 v1）
    import fcntl
    lockf = open(LOCK, "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another run in progress, exit")
        sys.exit(0)
    main()
