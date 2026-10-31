#!/usr/bin/env python3
"""todo_reminder.py — 待办超期提醒（no_agent，cron 每小时跑）。

扫描 /opt/data/待办/*.md，超过阈值天数未更新状态即输出提醒。
有超期待办才输出卡片（由 cron 投递或直发飞书），否则静默（0 token）。
状态阈值：进行中/观察中/待决策 统一 7 天（2026-08-29 主人定）。
用法: python3 /opt/data/scripts/todo_reminder.py
"""
import os, re, sys
from datetime import datetime

TODO_DIR = "/opt/data/待办"
# 状态 → 超期天数阈值（2026-09-02 升级：按优先级分档）
# 高优先 3 天 / 中优先 7 天 / 低优先 14 天（观察/待决策类低打扰，放宽）
THRESHOLD = {"进行中": {"高": 3, "中": 7, "低": 14},
             "观察中": {"高": 5, "中": 10, "低": 20},
             "待决策": {"高": 5, "中": 10, "低": 20},
             "已完结": {"高": 999, "中": 999, "低": 999}}

def parse_meta(content):
    """从文件头部解析状态/优先级/创建日期。"""
    meta = {"状态": "进行中", "优先级": "中", "创建": None}
    for line in content.splitlines()[:8]:
        m = re.match(r"^-\s*\*\*创建\*\*[:：]\s*(.+)$", line)
        if m: meta["创建"] = m.group(1).strip()
        m = re.match(r"^-\s*\*\*状态\*\*[:：]\s*(.+)$", line)
        if m: meta["状态"] = m.group(1).strip()
        m = re.match(r"^-\s*\*\*优先级\*\*[:：]\s*(.+)$", line)
        if m: meta["优先级"] = m.group(1).strip()
    return meta

def next_action(content):
    """从 ## 下一步 区段提取第一个 checkbox 动作（带建议文案）。"""
    lines = content.splitlines()
    in_next = False
    for line in lines:
        if line.startswith("## 下一步"):
            in_next = True
            continue
        if in_next and line.startswith("## "):
            break
        if in_next and re.match(r"^-\s*\[ \]", line):
            return line.lstrip("- [ ] ").strip()
    return None

def main():
    if not os.path.isdir(TODO_DIR):
        return 0
    now = datetime.now()
    overdue = []
    for f in sorted(os.listdir(TODO_DIR)):
        if not f.endswith(".md"):
            continue
        path = os.path.join(TODO_DIR, f)
        try:
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
        except Exception:
            continue
        meta = parse_meta(content)
        status = meta["状态"]
        pri = meta.get("优先级", "中")
        # 用文件 mtime 近似最后更新（比创建日期可靠）
        mtime = datetime.fromtimestamp(os.path.getmtime(path))
        age_days = (now - mtime).days
        th_map = THRESHOLD.get(status, THRESHOLD["进行中"])
        th = th_map.get(pri, 7)
        if status != "已完结" and age_days >= th:
            overdue.append((f, status, pri, age_days, th, next_action(content)))

    if not overdue:
        return 0  # 无超期，静默

    # 有超期 → 输出卡片（cron no_agent 投递 stdout 或直发飞书）
    lines = [f"📋 待办超期提醒（{len(overdue)} 条）", "━━━━━━━━━━━━━━━━━━━━"]
    for name, status, pri, age, th, act in overdue:
        lines.append(f"⏰ {name.replace('.md','')}")
        lines.append(f"　状态 {status}｜{pri}优先　已 {age} 天（阈值 {th} 天）")
        if act:
            lines.append(f"　→ 下一步：{act[:40]}")
    lines.append(f"🕐 {now.strftime('%m-%d %H:%M')}")

    # 直发飞书卡片（有脚本则用，无则 stdout 由 cron 投递）
    card = "\n".join(lines)
    # 测试守卫：TODO_REMINDER_DRYRUN=1 时不发卡片，只打印（2026-09-02 审查修复 G2）
    if os.environ.get("TODO_REMINDER_DRYRUN") == "1":
        print(card)
        return 0
    sys.path.insert(0, "/opt/data/scripts")
    try:
        from cron_notify import send_card
        send_card("📋", "待办超期提醒", lines[2:-1], profile="default")
    except Exception:
        print(card)
    return 0

if __name__ == "__main__":
    sys.exit(main())