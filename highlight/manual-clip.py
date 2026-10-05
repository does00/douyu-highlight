#!/usr/bin/env python3
"""人工选段后续：读取 queue/done/ 的精选结果，剪辑并投稿 B 站。

由 highlight-manual-pick 定时任务在选段后调用。
"""
import json, os, sys, glob, subprocess, time, importlib.util

BASE = os.path.dirname(os.path.abspath(__file__))
PEND = os.path.join(BASE, "queue", "pending")
DONE = os.path.join(BASE, "queue", "done")
WORKDIR = os.path.join(BASE, "highlights")

sys.path.insert(0, BASE)
# highlight-pipeline.py 带连字符，不能直接 import，用 importlib 按路径加载
_spec = importlib.util.spec_from_file_location(
    "highlight_pipeline", os.path.join(BASE, "highlight-pipeline.py"))
_hp = importlib.util.module_from_spec(_spec)
sys.modules["highlight_pipeline"] = _hp
_spec.loader.exec_module(_hp)
clip, upload, notify_qq_upload, log = _hp.clip, _hp.upload, _hp.notify_qq_upload, _hp.log

def snap_bounds(words, ws, we):
    """词边界吸附。words: [[t, tok], ...] 相对时间。"""
    for t, _ in words:
        if t >= ws - 5:
            ws = max(0.0, t - 0.5)
            break
    for t, _ in reversed(words):
        if t <= we + 5:
            we = t + 0.5
            break
    return ws, we

def process_done():
    if not os.path.isdir(DONE):
        return
    for fn in sorted(os.listdir(DONE)):
        if not fn.endswith(".json"):
            continue
        done_path = os.path.join(DONE, fn)
        pend_path = os.path.join(PEND, fn)
        try:
            result = json.load(open(done_path))
            picks = result.get("picks", [])
            if not picks:
                # 无精选，清理
                os.remove(done_path)
                if os.path.exists(pend_path):
                    os.remove(pend_path)
                continue
            if not os.path.exists(pend_path):
                log(f"  找不到对应的 pending 文件: {fn}")
                os.remove(done_path)
                continue
            meta = json.load(open(pend_path))
            ts_path = meta.get("ts_path", "")
            if not ts_path or not os.path.exists(ts_path):
                log(f"  录像文件不存在: {ts_path}")
                continue
            words = meta.get("words", [])
            streamer = meta.get("streamer", "")
            room_id = meta.get("room_id", "")
            chunk_off = meta.get("offset", 0)

            # 读房间配置拿标题格式等
            cfg = json.load(open(os.path.join(BASE, "highlight-rooms.json")))
            room = next((r for r in cfg["rooms"] if str(r.get("room_id")) == str(room_id)), {})

            for p in picks:
                s_rel, e_rel = float(p["s"]), float(p["e"])
                title_suffix = p.get("title", "精彩片段")
                # 转绝对时间
                ws, we = chunk_off + s_rel, chunk_off + e_rel
                # 词边界吸附（用 chunk 内相对坐标）
                cws = [[t, tok] for t, tok in words]
                ws_r, we_r = snap_bounds(cws, s_rel, e_rel)
                ws, we = chunk_off + ws_r, chunk_off + we_r
                # 时长由人工判断，不设硬性下限
                # 生成文件名和标题
                ts_base = os.path.basename(ts_path)
                # 从文件名解析时间
                import re
                m = re.search(r"(\d{4}-\d{2}-\d{2})[ _](\d{2})-(\d{2})", ts_base)
                date_str = f"{m.group(1)[5:7]}-{m.group(1)[8:10]}" if m else time.strftime("%m-%d")
                out_name = f"{streamer}_{date_str}_{int(ws)}_精彩_{title_suffix[:20]}.mp4"
                out_path = os.path.join(WORKDIR, out_name)
                os.makedirs(WORKDIR, exist_ok=True)
                log(f"  剪辑 [{ws:.0f}-{we:.0f}] {title_suffix}")
                if clip(ts_path, ws, we, out_path):
                    title = f"[{streamer}] {title_suffix} {date_str}"
                    log(f"  投稿: {title}")
                    # 简介：推荐理由 + 房间地址
                    why = p.get("why", "")
                    room_url = f"https://www.douyu.com/{room_id}"
                    desc = f"{why}\n\n直播间：{room_url}" if why else f"直播间：{room_url}"
                    try:
                        upload(out_path, title, desc=desc)
                        notify_qq_upload(title, we - ws, streamer)
                        log(f"  投稿成功")
                    except Exception as e:
                        log(f"  投稿失败: {e}")
                else:
                    log(f"  剪辑失败，跳过")
            # 处理完清理
            os.remove(done_path)
            os.remove(pend_path)
        except Exception as e:
            log(f"  处理 {fn} 异常: {e}")
            import traceback
            log(traceback.format_exc()[-300:])

if __name__ == "__main__":
    process_done()
