"""归档：规则选段代码（2026-10-03 已禁用，2026-10-05 归档）。"""

def find_candidates(words, danmaku, energy=None, max_keep=6):
    """滑动窗口打分 + 自适应阈值 + 非极大值抑制。
    返回 [(ws, we, score, reasons, title)]（按时间排序）。
    自适应：先按 KEEP_SCORE 过滤；过线不足 max_keep 个时，放宽到 ABS_FLOOR
    按分数补足（文静场次也能选出相对最佳时刻，挂机垃圾场次仍被下限挡掉）。"""
    if not words:
        return []
    bursts, thr = danmaku_bursts(danmaku)
    dur = words[-1][0]
    # 音频能量全局统计（供窗口内突增判断）
    estats = None
    if energy:
        rms = np.array([r for _, r in energy], dtype=np.float64)
        if len(rms):
            estats = (float(rms.mean()), float(rms.std()),
                      float(np.percentile(rms, 90)))
    # 全部窗口打分（先不过滤）
    scored = []
    ws = 0.0
    while ws + WIN_SEC <= dur + WIN_STEP:
        we = min(ws + WIN_SEC, dur)
        score, reasons, _ = score_window(ws, we, words, danmaku, bursts, energy, estats)
        scored.append((ws, we, score, reasons))
        ws += WIN_STEP
    if not scored:
        return []
    ABS_FLOOR = 3.0  # 绝对下限：低于此分视为垃圾，不剪
    strict = [s for s in scored if s[2] >= KEEP_SCORE]
    if len(strict) >= max_keep:
        picked = strict
    else:
        # 按分数降序，全部 >= ABS_FLOOR 的都交给 NMS（它自己会取 top，不在这里预切片，
        # 否则高分窗口互重叠时 NMS 去重后数量不足）
        scored.sort(key=lambda x: -x[2])
        picked = [s for s in scored if s[2] >= ABS_FLOOR]
        if not picked:
            picked = strict  # 兜底：不应发生（strict 为空才会进此分支）
    cands = [(ws, we, sc, rs, None) for ws, we, sc, rs in picked]
    return nms(cands, max_keep)
