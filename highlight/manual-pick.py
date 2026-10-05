#!/usr/bin/env python3
"""人工选段工具：列出待选队列，供 Muse 阅读后写回结果。

用法:
  python3 manual-pick.py list              # 列出待选块
  python3 manual-pick.py show <文件名>      # 显示某块的转写+弹幕
  python3 manual-pick.py done <文件名>     # 标记已处理（无精选）
  # 选中后，把结果 JSON 写到 queue/done/<同名>.json，格式:
  # {"picks": [{"s": 12.5, "e": 185.0, "title": "标题", "why": "理由"}]}
  # s/e 是块内相对秒数
"""
import json, os, sys

BASE = os.path.dirname(os.path.abspath(__file__))
PEND = os.path.join(BASE, "queue", "pending")
DONE = os.path.join(BASE, "queue", "done")

def cmd_list():
    files = sorted(os.listdir(PEND)) if os.path.isdir(PEND) else []
    if not files:
        print("队列为空")
        return
    for fn in files:
        d = json.load(open(os.path.join(PEND, fn)))
        n_dm = len(d.get("danmaku", []))
        print(f"{fn} | {d.get('streamer')} | {len(d.get('text',''))}字 | {n_dm}条弹幕")

def cmd_show(fn):
    d = json.load(open(os.path.join(PEND, fn)))
    print(f"=== {d.get('streamer')} ({d.get('room_id')}) 块{ d.get('chunk_idx')} ===\n")
    print("--- 转写 ---")
    print(d.get("text", "")[:8000])
    print("\n--- 弹幕 (前50) ---")
    for m in d.get("danmaku", [])[:50]:
        print(f"[{m['t']:6.1f}s] {m['user']}: {m['text']}")

def cmd_done(fn):
    # 无精选，移到 done 空结果
    src = os.path.join(PEND, fn)
    os.makedirs(DONE, exist_ok=True)
    with open(os.path.join(DONE, fn), "w") as f:
        json.dump({"picks": []}, f)
    os.remove(src)
    print(f"已标记无精选: {fn}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
    elif sys.argv[1] == "list":
        cmd_list()
    elif sys.argv[1] == "show" and len(sys.argv) > 2:
        cmd_show(sys.argv[2])
    elif sys.argv[1] == "done" and len(sys.argv) > 2:
        cmd_done(sys.argv[2])
