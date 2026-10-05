#!/usr/bin/env python3
"""
LLM 精彩片段挑选（Phase 2）。

约束：gemini-relay 单次输出约 60-80 字符后会被截断（实测），
因此 LLM 只做最小化输出，分两步：
  1. 发现：每 10min 块 → 返回紧凑 JSON [{"s":45,"e":135}]（无标题/理由）
  2. 标题：对最终候选逐个起 15 字内标题（短输出，不截断）

防幻觉：发现阶段校验时间段内确有语音（词密度）；标题阶段不影响切点。
失败 fail-open，上游用规则候选兜底。

token 从 ~/workspace/douyu-ai-bot/.env 读 RELAY_TOKEN，不进代码不打印。
"""
import json
import os
import re
import time
import urllib.request
import urllib.error

BOT_ENV = os.environ.get("BOT_ENV", "/home/hatch/workspace/douyu-ai-bot/.env")
CHUNK_SEC = 600
LLM_MIN_CLIP = 180.0
LLM_MAX_CLIP = 600.0
LLM_TIMEOUT = 60
CHUNK_DELAY = 6.0


def _load_relay():
    # 环境变量优先（Docker），回退读 douyu-ai-bot/.env（本机）
    url = os.environ.get("LLM_RELAY_URL", "").strip()
    token = os.environ.get("LLM_RELAY_TOKEN", "").strip()
    if not url or not token:
        try:
            with open(BOT_ENV, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("GEMINI_RELAY_URL=") and not url:
                        url = line.split("=", 1)[1].strip()
                    elif line.startswith("RELAY_TOKEN=") and not token:
                        token = line.split("=", 1)[1].strip()
        except OSError:
            pass
    # 兼容旧变量名
    if not url:
        url = os.environ.get("GEMINI_RELAY_URL", "").strip()
    if not url:
        url = "http://127.0.0.1:18020/generate"
    return url, token


RELAY_URL, RELAY_TOKEN = _load_relay()
LLM_OK = bool(RELAY_TOKEN)

# 手动选段模式：1=跳过 LLM，存队列等人工选
MANUAL_MODE = os.environ.get("HIGHLIGHT_MANUAL", "0") == "1"
QUEUE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "queue")

# 直连模式：NAS 部署用
# Provider 二选一：deepseek（默认）或 gemini
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "deepseek").strip().lower()
# DeepSeek（OpenAI 兼容接口）
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat").strip()
DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE_URL",
                               "https://api.deepseek.com/v1").strip()
# Gemini（备用）
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview").strip()
# 代理：国内需要，格式 http://host:port（局域网 v2ray）
HTTP_PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""

if LLM_PROVIDER == "deepseek":
    LLM_OK = bool(DEEPSEEK_API_KEY)
    LLM_MODEL = DEEPSEEK_MODEL
elif LLM_PROVIDER == "gemini":
    LLM_OK = bool(GEMINI_API_KEY)
    LLM_MODEL = GEMINI_MODEL
else:
    # 中继模式（本机）
    LLM_OK = bool(RELAY_TOKEN)
    LLM_MODEL = "gemini-3-flash-preview"

USE_DIRECT = LLM_PROVIDER in ("deepseek", "gemini") and LLM_OK


def _call_deepseek(prompt, max_tokens=800):
    """DeepSeek API（OpenAI 兼容），国内直连无需代理。"""
    import urllib.request
    url = f"{DEEPSEEK_BASE}/chat/completions"
    body = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }).encode()
    # DeepSeek 国内可直连，不走代理
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {DEEPSEEK_API_KEY}"})
    with opener.open(req, timeout=LLM_TIMEOUT) as r:
        j = json.load(r)
    try:
        return j["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return ""


def _call_direct(prompt, max_tokens=800):
    """直连 Gemini API（经代理）。"""
    import urllib.request
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.7},
    }).encode()
    handlers = []
    if HTTP_PROXY:
        handlers.append(urllib.request.ProxyHandler(
            {"http": HTTP_PROXY, "https": HTTP_PROXY}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=LLM_TIMEOUT) as r:
        j = json.load(r)
    try:
        return j["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return ""


def _fmt_ts(sec):
    return "%02d:%02d" % (int(sec // 60), int(sec % 60))



def _call_relay(prompt, system="x", max_tokens=800, retries=3):
    body = json.dumps({
        "prompt": prompt,
        "system": system,
        "model": "gemini-3-flash-preview",
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }).encode()
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(
            RELAY_URL, data=body,
            headers={"Content-Type": "application/json",
                     "X-Relay-Token": RELAY_TOKEN})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=LLM_TIMEOUT) as r:
                j = json.load(r)
            if isinstance(j, dict):
                return j.get("text") or j.get("response") or j.get("content") or ""
            return str(j)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 502, 503) and attempt < retries - 1:
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise last_err



def _get_clip_range():
    """从 highlight-rooms.json 读当前房间的 min/max_clip（取第一个启用的房间）。"""
    try:
        import json as _json, os as _os
        cfg_path = _os.path.join(_os.path.dirname(__file__), "highlight-rooms.json")
        cfg = _json.load(open(cfg_path))
        for r in cfg.get("rooms", []):
            if r.get("enabled", True):
                clip = r.get("clip", {})
                mn = int(clip.get("min_clip", 180))
                mx = int(clip.get("max_clip", 600))
                return mn, mx
    except Exception:
        pass
    return int(LLM_MIN_CLIP), int(LLM_MAX_CLIP)

def _get_custom_prompt():
    """从 highlight-rooms.json 读用户自定义提示词（AI配置页）。"""
    try:
        import json as _json, os as _os
        cfg_path = _os.path.join(_os.path.dirname(__file__), "highlight-rooms.json")
        cfg = _json.load(open(cfg_path))
        p = (cfg.get("ai_global", {}) or {}).get("prompt", "")
        return p.strip() if p else ""
    except Exception:
        return ""


def pick_highlights_llm(words, danmaku, log=print, room_id="", streamer="", ts_path="", session_base=0):
    """LLM 发现阶段。返回 [(ws, we, score, reasons, None)]（标题后续单独生成）。
    MANUAL_MODE=1 时存队列等人工选，返回 []。"""
    if MANUAL_MODE:
        _save_to_queue(words, danmaku, room_id, streamer, log, ts_path, session_base)
        return []
    if not LLM_OK:
        log("LLM unavailable, skipping")
        return []
    if not words:
        return []
    dur = words[-1][0]
    cands = []
    off, idx = 0.0, 0
    while off < dur:
        ce = min(off + CHUNK_SEC, dur)
        cw = [(t - off, tok) for t, tok in words if off <= t < ce]
        chunk_text = "".join(tok for _, tok in cw)
        if len(chunk_text) < 100:
            off += CHUNK_SEC
            idx += 1
            continue
        cdm = [(t - off, u, x) for t, u, _, x in danmaku if off - 20 <= t < ce]
        try:
            mt = 800 if LLM_PROVIDER == "deepseek" else 120
            resp = _call_llm(build_discover_prompt(cw, cdm), max_tokens=mt)
        except Exception as e:
            log("LLM discover failed @chunk%d: %s" % (idx, e))
            off += CHUNK_SEC
            idx += 1
            continue
        raw = _extract_json_array(resp)
        ok = 0
        for c in raw[:2]:
            try:
                s, e = float(c["s"]), float(c["e"])
            except (TypeError, ValueError, KeyError):
                continue
            mn, mx = _get_clip_range()
            if not (0 <= s < e <= CHUNK_SEC and mn <= e - s <= mx):
                continue
            ws, we = off + s, off + e
            # 防幻觉：时间段内必须有足够语音
            nwords = sum(1 for t, _ in words if ws <= t <= we)
            if nwords < 20:
                continue
            # 吸附词边界
            for t, _ in words:
                if t >= ws - 5:
                    ws = max(0.0, t - 0.5)
                    break
            for t, _ in reversed(words):
                if t <= we + 5:
                    we = t + 0.5
                    break
            if we - ws < 30:
                continue
            dm_hit = any(ws - 20 <= t <= we + 5 for t, _, _, _ in danmaku
                         if off <= t < ce)
            # LLM 基分 20，确保优先于规则版（规则版一般 13-17 分）
            # 这样 LLM 的起止时长选择不会被规则窗口覆盖
            score = 20.0 + (1.0 if dm_hit else 0.0)
            reasons = ["LLM-pick"]
            if dm_hit:
                reasons.append("danmaku+1")
            llm_title = c.get("title", "").strip() if isinstance(c.get("title"), str) else ""
            llm_why = c.get("why", "").strip() if isinstance(c.get("why"), str) else ""
            if llm_why:
                reasons.append(llm_why[:100])
            cands.append((ws, we, score, reasons, llm_title or None))
            ok += 1
        log("LLM chunk%d [%d,%d]: %d raw -> %d valid"
            % (idx, off, ce, len(raw), ok))
        off += CHUNK_SEC
        idx += 1
        if off < dur:
            time.sleep(CHUNK_DELAY)
    return cands


def generate_title(excerpt, log=print):
    """给一段转写摘录起 4-6 字中文标题（中继只支持超短输出）。
    返回标题或 None。"""
    if not LLM_OK or not excerpt:
        return None
    prompt = ("给下面这段游戏直播语音转写起一个4到6字的中文视频标题，"
              "要有网感，只返回标题本身，不要标点：\n" + excerpt[:200])
    try:
        resp = _call_llm(prompt, max_tokens=30)
    except Exception as e:
        log("LLM title failed: %s" % e)
        return None
    t = resp.strip().strip('"“”').strip()
    t = re.split(r"[\n：:，。！？]", t)[0].strip().strip('1234567890.、"“” ')
    # DeepSeek 不截断，放宽到 12 字；Gemini 保持 8 字防截断残片
    max_len = 12 if LLM_PROVIDER == "deepseek" else 8
    if 2 <= len(t) <= max_len:
        return t[:max_len]
    log(f"标题长度不合规被过滤 [{len(t)}字]: {t[:20]}")
    return None


def generate_title_and_desc(excerpt, log=print):
    """标题（LLM）+ 简介（模板回退）。
    中继不支持长输出，简介用固定模板。返回 (title, None)。"""
    title = generate_title(excerpt, log=log)
    return title, None


def _save_to_queue(words, danmaku, room_id, streamer, log=print, ts_path="", session_base=0):
    """手动模式：把转写+弹幕存到队列，等人工选段。
    ts_path: 对应的录像文件；session_base: chunk offset 对应 ts 内的绝对偏移。"""
    import time, json
    pend = os.path.join(QUEUE_DIR, "pending")
    os.makedirs(pend, exist_ok=True)
    # 10分钟分块存
    dur = words[-1][0] if words else 0
    off = 0.0
    idx = 0
    saved = 0
    while off < dur:
        ce = min(off + 600, dur)
        cw = [(t - off, tok) for t, tok in words if off <= t < ce]
        chunk_text = "".join(tok for _, tok in cw)
        if len(chunk_text) >= 100:
            cdm = [(t - off, u, x) for t, u, _, x in danmaku if off - 20 <= t < ce]
            # 转写文本带时间戳（每30秒一个标记）
            lines = []
            cur_mark = -1
            for t_rel, tok in cw:
                mark = int(t_rel // 30) * 30
                if mark != cur_mark:
                    lines.append(f"\n[{mark//60:02d}:{mark%60:02d}] ")
                    cur_mark = mark
                lines.append(tok)
            fn = f"{room_id}_{int(time.time())}_{idx}.json"
            data = {
                "room_id": room_id, "streamer": streamer,
                "chunk_idx": idx, "offset": off,
                "ts_path": ts_path, "session_base": session_base,
                "text": "".join(lines),
                "words": [[round(t, 2), tok] for t, tok in cw],
                "danmaku": [{"t": round(t, 1), "user": u, "text": x} for t, u, x in cdm[:200]],
            }
            with open(os.path.join(pend, fn), "w") as f:
                json.dump(data, f, ensure_ascii=False)
            saved += 1
        off += 600
        idx += 1
    log(f"手动模式：已存 {saved} 个待选块到队列")
