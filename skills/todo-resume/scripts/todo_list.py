#!/usr/bin/env python3
"""todo_list.py — 看待办列表（一次性列出，省多次 read_file）。

用法: python3 /opt/data/scripts/todo_list.py [状态筛选] [--snapshot] [--done]
  - 无参：列出全部待办（按优先级动态排序：高→中→低，同级按建档时间新→旧）
  - 带状态词：筛选（如 '观察中' '待决策'）
  - --snapshot：输出面板同时写序号→文件映射快照（供「处理第N条」按面板序号精准定位，防排序漂移）
  - --done：列「已完成/」目录（已完结留档），快照写 .last_panel_done.json
输出精简清单：序号 + 状态图标 + 标题 + 优先级 + 建档时间 + 一句话当前状态。
"""

import json, os, re, sys
from datetime import datetime

TODO_DIR = "/opt/data/待办"
DONE_DIR = "/opt/data/待办/已完成"
SNAPSHOT_FILE = "/opt/data/待办/.last_panel.json"
SNAPSHOT_DONE_FILE = "/opt/data/待办/.last_panel_done.json"
# 优先级排序权重（高优先最前）
PRIORITY = {"高": 0, "中": 1, "低": 2}
# 状态排序权重（观察类最优先）— 仅作同优先级同建档时间下的次级排序
ORDER = {"观察中": 0, "待决策": 1, "进行中": 2, "已完结": 3}
ICON = {"进行中": "🟢", "观察中": "👀", "待决策": "🤔", "已完结": "✅"}

def _date_int(d):
    """日期字符串转整数便于排序（缺失记 0）。"""
    try:
        return int(d.replace("-", ""))
    except (AttributeError, ValueError):
        return 0

def parse(f):
    """从 md 头部提取 创建日期/状态/优先级/目标/当前状态。"""
    with open(f, encoding="utf-8") as fh:
        content = fh.read()
    meta = {"创建": "", "状态": "进行中", "优先级": "中", "目标": "", "当前": ""}
    for line in content.splitlines()[:15]:
        m = re.match(r"^-\s*\*\*创建\*\*[:：]\s*(\d{4}-\d{2}-\d{2})", line)
        if m: meta["创建"] = m.group(1).strip()
        m = re.match(r"^-\s*\*\*状态\*\*[:：]\s*(.+)$", line)
        if m: meta["状态"] = m.group(1).strip()
        m = re.match(r"^-\s*\*\*优先级\*\*[:：]\s*(.+)$", line)
        if m: meta["优先级"] = m.group(1).strip()
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^## 目标\s*$", line) and i + 1 < len(lines):
            meta["目标"] = lines[i + 1].strip().lstrip("- ")
        if re.match(r"^## 当前状态\s*$", line) and i + 1 < len(lines):
            meta["当前"] = lines[i + 1].strip().lstrip("- ")
    return meta

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    snapshot = "--snapshot" in sys.argv[1:]
    done = "--done" in sys.argv[1:]
    filt = args[0] if args else None
    base_dir = DONE_DIR if done else TODO_DIR
    if not os.path.isdir(base_dir):
        print("待办目录不存在"); return 0
    items = []
    for name in os.listdir(base_dir):
        if not name.endswith(".md"): continue
        meta = parse(os.path.join(base_dir, name))
        if filt and filt not in meta["状态"]: continue
        items.append((name, meta))
    # 动态排序：优先级（高→低）为主键，建档时间（新→旧）为次键，状态为末键
    items.sort(key=lambda x: (
        PRIORITY.get(x[1]["优先级"], 3),
        -_date_int(x[1]["创建"]),
        ORDER.get(x[1]["状态"], 3),
    ))
    if not items:
        print("（无待办）")
        if snapshot:
            _write_snapshot([], done)
        return 0
    lines = []
    for idx, (name, meta) in enumerate(items, 1):
        icon = ICON.get(meta["状态"], "•")
        title = name.replace(".md", "")
        pri_mark = {"高": "🔴", "中": "🟡", "低": "⚪"}.get(meta["优先级"], "")
        created = meta["创建"][5:] if len(meta["创建"]) >= 10 else "—"
        summary = _clean_summary(meta["当前"] or meta["目标"])
        # 两行制（2026-09-05 主人反馈排版）：首行=序号+图标+标题+优先级；次行=建档+状态摘要
        lines.append(f"{idx}. {icon} {title} {pri_mark}")
        if summary:
            lines.append(f"　  📅{created} · {summary}")
    print("\n".join(lines))
    if snapshot:
        _write_snapshot([{"seq": i, "file": n, "title": n.replace(".md", ""),
                          "status": m["状态"], "priority": m["优先级"],
                          "created": m["创建"], "summary": _clean_summary(m["当前"] or m["目标"])}
                         for i, (n, m) in enumerate(items, 1)], done)
    return 0

def _clean_summary(s):
    """清洗状态摘要：去 markdown 符号、压缩空白、截断 45 字。"""
    if not s:
        return ""
    s = re.sub(r"[*_`~#]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > 45:
        s = s[:45] + "…"
    return s

def _write_snapshot(snapshot_items, done=False):
    """写 序号→文件 快照（面板生成时固化，处理第N条按此定位防漂移；已完成另写一份防串号）。"""
    try:
        data = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "scope": "done" if done else "active",
            "count": len(snapshot_items),
            "items": snapshot_items,
        }
        path = SNAPSHOT_DONE_FILE if done else SNAPSHOT_FILE
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        # 快照写失败不影响面板输出
        sys.stderr.write(f"[todo_list] snapshot 写失败: {e}\n")

if __name__ == "__main__":
    sys.exit(main())
