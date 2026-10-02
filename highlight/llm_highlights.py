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

BOT_ENV = "/home/hatch/workspace/douyu-ai-bot/.env"
CHUNK_SEC = 600.0
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


def _extract_json_array(text):
    """提取 JSON 数组；截断时抢救完整对象。"""
    m = re.search(r"(\[.*\])", text, re.S)
    if not m:
        return []
    try:
        d = json.loads(m.group(1))
        return d if isinstance(d, list) else []
    except Exception:
        pass
    # 截断抢救：逐个提取完整 {...}
    out = []
    for om in re.finditer(r"\{[^{}]*\}", m.group(1)):
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
            resp = _call_relay(build_discover_prompt(cw, cdm), max_tokens=120)
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
            if not (0 <= s < e <= CHUNK_SEC and 30 <= e - s <= 200):
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
            score = 8.0 + (1.0 if dm_hit else 0.0)
            reasons = ["LLM-pick"]
            if dm_hit:
                reasons.append("danmaku+1")
            cands.append((ws, we, score, reasons, None))
            ok += 1
        log("LLM chunk%d [%d,%d]: %d raw -> %d valid"
            % (idx, off, ce, len(raw), ok))
        off += CHUNK_SEC
        idx += 1
        if off < dur:
            time.sleep(CHUNK_DELAY)
    return cands


def generate_title(excerpt, log=print):
    """给一段转写摘录起 15 字内中文标题。返回标题或 None。"""
    if not LLM_OK or not excerpt:
        return None
    prompt = ("给下面这段游戏直播语音转写起一个15字内的中文视频标题，"
              "要有网感，只返回标题本身：\n" + excerpt[:300])
    try:
        resp = _call_relay(prompt, max_tokens=60)
    except Exception as e:
        log("LLM title failed: %s" % e)
        return None
    t = resp.strip().strip('"“”').strip()
    # 取第一行，去掉可能的序号/引号
    t = re.split(r"[\n：:]", t)[0].strip().strip('1234567890.、"“”')
    return t[:20] if len(t) >= 2 else None
