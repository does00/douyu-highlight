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
LLM_MIN_CLIP = 30.0
LLM_MAX_CLIP = 300.0
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


def _call_llm(prompt, system="x", max_tokens=800, retries=3):
    """统一入口：直连模式（deepseek/gemini）优先，否则走中继（本机）。"""
    if USE_DIRECT:
        fn = _call_deepseek if LLM_PROVIDER == "deepseek" else _call_direct
        last_err = None
        for attempt in range(retries):
            try:
                return fn(prompt, max_tokens)
            except Exception as e:
                last_err = e
                if attempt < retries - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
        raise last_err
    return _call_relay(prompt, system, max_tokens, retries)


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


def _extract_json_array(text):
    """提取 JSON 数组；截断时抢救完整对象。"""
    # 先找完整的 [...]，找不到则从第一个 [ 开始截断抢救
    m = re.search(r"(\[.*\])", text, re.S)
    blob = m.group(1) if m else None
    if not blob:
        i = text.find("[")
        blob = text[i:] if i >= 0 else None
    if not blob:
        return []
    try:
        d = json.loads(blob)
        return d if isinstance(d, list) else []
    except Exception:
        pass
    # 截断抢救：逐个提取完整 {...}
    out = []
    for om in re.finditer(r"\{[^{}]*\}", blob):
        try:
            d = json.loads(om.group(0))
            if isinstance(d, dict):
                out.append(d)
        except Exception:
            continue
    return out


def build_discover_prompt(chunk_words, chunk_danmaku):
    lines, cur, acc = [], None, []
    for t, tok in chunk_words:
        b = int(t // 15) * 15
        if cur is None:
            cur = b
        if b != cur:
            lines.append("[%s] %s" % (_fmt_ts(cur), "".join(acc)))
            cur, acc = b, []
        acc.append(tok)
    if acc:
        lines.append("[%s] %s" % (_fmt_ts(cur), "".join(acc)))
    transcript = "\n".join(lines)
    dm = "\n".join("[%s] %s" % (_fmt_ts(t), x)
                   for t, _, x in chunk_danmaku[:40])
    if LLM_PROVIDER == "deepseek":
        dur_str = f"{int(LLM_MIN_CLIP)}-{int(LLM_MAX_CLIP)}"
        return (
            "你是一个游戏直播精彩片段剪辑师。下面是某主播约10分钟直播的语音转写（含时间戳）"
            "和同期弹幕。\n"
            "[转写]\n" + transcript +
            "\n[弹幕]\n" + (dm if dm else "(无)") +
            "\n请挑选 0-2 个最精彩的片段（高能操作、爆笑时刻、金句、神反转等）。"
            "只返回 JSON 数组，不要解释，不要 markdown：\n"
            '[{"s":45,"e":135,"title":"标题不超过12字","why":"一句话理由"}]\n'
            f"要求：s/e 为相对本段起始的秒数；每段 {dur_str} 秒；"
            "时间必须落在有语音的区间；弹幕爆发可作参考但别只看弹幕。"
        )
    return (
        "Douyu game livestream transcript (timestamps) + danmaku below.\n"
        "[TRANSCRIPT]\n" + transcript +
        "\n[DANMAKU]\n" + (dm if dm else "(none)") +
        "\nPick 0-2 highlight clips (epic/funny/quotes). "
        "Reply ONLY compact JSON like [{\"s\":45,\"e\":135}]. "
        "s/e=seconds from chunk start, clip 30-180s. "
        "No markdown, no explanation."
    )


def pick_highlights_llm(words, danmaku, log=print):
    """LLM 发现阶段。返回 [(ws, we, score, reasons, None)]（标题后续单独生成）。"""
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
            if not (0 <= s < e <= CHUNK_SEC and LLM_MIN_CLIP <= e - s <= LLM_MAX_CLIP):
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
