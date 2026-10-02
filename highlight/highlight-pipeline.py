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

def transcript_cache_path(ts_path):
    safe = re.sub(r"[^\w\-.]", "_", os.path.basename(ts_path))
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
        json.dump({"size": st.st_size, "mtime": st.st_mtime,
                   "chunks": {str(k): v for k, v in chunks.items()}},
                  open(cp, "w"), ensure_ascii=False)
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
    """非极大值抑制：按分数降序贪心取不重叠窗口。cands 元素为 (ws,we,score,reasons,title)。"""
    cands = sorted(cands, key=lambda x: -x[2])
    kept = []
    for c in cands:
        ws, we = c[0], c[1]
        if all(we <= k[0] or ws >= k[1] for k in kept):
            kept.append(c)
    kept.sort()
    return kept[:max_keep]

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

def upload(path, title):
    import urllib.request
    passkey = BILILIVE_PASSKEY or json.load(open(BILILIVE_CONFIG))["passKey"]
    payload = {"uid": UID, "videos": [path], "config": {
        "title": title,
        "desc": f"斗鱼房间 {ROOM_ID} 直播精彩片段（内容+弹幕双信号自动剪辑，仅自己可见）",
        "tag": ["精彩片段", "录播", "游戏直播"],
        "tid": 17, "human_type2": 1008, "copyright": 2,
        "source": f"https://www.douyu.com/{ROOM_ID}",
        "dolby": 0, "hires": 0, "is_only_self": 1,
        "noReprint": 0, "closeDanmu": 0, "closeReply": 0, "no_disturbance": 1},
        "options": {"removeOriginAfterUploadCheck": True}}
    req = urllib.request.Request(f"{BILILIVE_API}/bili/upload",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": passkey})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["taskId"]

# ---------------- 主流程 ----------------
def main():
    os.makedirs(WORKDIR, exist_ok=True)
    if os.path.exists(STATE):
        state = json.load(open(STATE))
    else:
        state = {"processed": [], "uploaded": []}
    processed = set(state.get("processed", []))

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
        if os.path.getmtime(p) < now - 360:
            tss.append(p)
    if not tss:
        log("no new ts, exit")
        if not DRY_RUN:
            state["processed"] = sorted(processed)
            json.dump(state, open(STATE, "w"))
        return

    for ts_path in tss:
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
            cands = find_candidates(words, danmaku)
            # Phase 2: LLM 挑段（失败则跳过，用规则候选兜底）
            if "--no-llm" not in sys.argv:
                try:
                    from llm_highlights import pick_highlights_llm, LLM_OK
                    if LLM_OK:
                        log("  LLM 挑选中...")
                        llm_cands = pick_highlights_llm(words, danmaku, log=log)
                        log(f"  LLM 候选 {len(llm_cands)} 个")
                        cands = nms(cands + llm_cands)  # 融合去重（LLM 基分 8，优先）
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
            if rec_start:
                clip_t = rec_start + timedelta(seconds=cs)
            else:
                clip_t = datetime.now(cst)
            tstr = clip_t.strftime("%H%M")
            day = clip_t.strftime("%Y-%m-%d")
            out = os.path.join(WORKDIR, f"{STREAMER}_{day}_精彩_{tstr}.mp4")
            if not clip(ts_path, cs, ce, out):
                log(f"  剪辑失败 [{cs:.0f},{ce:.0f}]"); continue
            # 标题：LLM 生成优先（短输出，不截断），失败回退时间标题
            title = None
            if "--no-llm" not in sys.argv and words:
                try:
                    from llm_highlights import generate_title
                    excerpt = "".join(tok for t, tok in words if ws <= t <= we)
                    title = generate_title(excerpt, log=log)
                except Exception as e:
                    log(f"  标题生成失败: {e}")
            if title:
                title = f"{STREAMER} {day} {title}"
            else:
                title = f"{STREAMER} {day} 直播精彩片段 {tstr}"
            try:
                task = upload(out, title)
                log(f"  已投稿: {title} ({ce-cs:.0f}s) task={task}")
                state.setdefault("uploaded", []).append(
                    {"title": title, "start": cs, "end": ce, "score": sc,
                     "reasons": rs, "task": task})
            except Exception as e:
                log(f"  投稿失败 {title}: {e}")

        processed.add(base)
        # 清理：删 xml；删 6 分钟无变动的 .ts（已处理完）
        if os.path.exists(xml_path):
            try: os.remove(xml_path)
            except OSError: pass
        if os.path.exists(ts_path) and now - os.path.getmtime(ts_path) > 360:
            try:
                os.remove(ts_path)
                log(f"  删除已处理分段: {base[:30]}")
            except OSError as e:
                log(f"  删除分段失败: {e}")

    if not DRY_RUN:
        state["processed"] = sorted(processed)
        json.dump(state, open(STATE, "w"))
    log("done")

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
