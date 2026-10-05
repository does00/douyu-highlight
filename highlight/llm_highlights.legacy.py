"""归档：外部 AI 调用代码（手动模式下停用，2026-10-05）。
如需切回 AI 选段，从此文件恢复。"""

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
        mn, mx = _get_clip_range()
        dur_str = f"{mn}-{mx}"
        # 固定前缀
        FIXED_PREFIX = ("你是一个游戏直播精彩片段剪辑师，眼光毒辣，宁缺毋滥。"
                        "下面是直播的语音转写（含时间戳）和同期弹幕。入选标准如下\n")
        custom = _get_custom_prompt()
        if custom:
            return (
                FIXED_PREFIX + custom + "\n[转写]\n" + transcript +
                "\n[弹幕]\n" + (dm if dm else "(无)") +
                "\n只返回 JSON 数组，不要解释，不要 markdown：\n"
                '[{"s":45,"e":135,"title":"标题不超过12字","why":"一句话理由"}]\n'
                f"要求：s/e 为相对本段起始的秒数；每段 {dur_str} 秒；"
                "时间必须落在有语音的区间；弹幕爆发可作参考但别只看弹幕。"
            )
        return (
            "你是一个游戏直播精彩片段剪辑师。下面是某主播约10分钟直播的语音转写（含时间戳）"
            "和同期弹幕。\n"
            "[转写]\n" + transcript +
            "\n[弹幕]\n" + (dm if dm else "(无)") +
            "\n请挑选 0-2 个最精彩的片段。标准要严：必须是高能操作、爆笑名场面、神反转、情绪大爆发这种让观众忍不住看完的时刻。"
            "日常唠嗑、碎碎念、纠结选啥、算数、闲聊一律不要选，宁可返回空数组也别凑数。"
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

