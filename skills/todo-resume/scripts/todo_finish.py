#!/usr/bin/env python3
"""todo_finish.py — 完结待办一条龙（2026-09-02 新增）。

一条命令走完完结四步：
  1. 追加 ## 总结 区段（--summary 传入，否则交互输入）
  2. 校验「关联」里的路径是否存在（只警告，不阻断）
  3. 清理标记的临时路径（--clean-tmp 显式才删，重要产物提示主人确认）
  4. 头部状态改为「已完结」，移入 已完成/ 子目录留档

用法:
  python3 /opt/data/scripts/todo_finish.py <标题或序号> [--summary "总结"]
  python3 /opt/data/scripts/todo_finish.py <标题或序号> --summary "..." --clean-tmp

注意:
  - 只处理 /opt/data/待办/ 根目录下的文件（已完成/ 下不算）
  - 完结后输出「建议记忆摘要」供主人确认，不自动写入 memory
"""
import os, re, shutil, sys
from datetime import datetime

TODO_DIR = "/opt/data/待办"
DONE_DIR = os.path.join(TODO_DIR, "已完成")

def find_file(target):
    """按标题或序号找待办文件。返回绝对路径或 None。"""
    if target.isdigit():
        # 序号模式：与 todo_list.py 完全一致的排序（优先级→建档时间→状态），避免序号错位
        idx = int(target)
        files = [f for f in os.listdir(TODO_DIR) if f.endswith(".md")]
        # 与 todo_list.py 同款解析与排序
        def _date_int(d):
            try:
                return int(d.replace("-", ""))
            except (AttributeError, ValueError):
                return 0
        pri_w = {"高": 0, "中": 1, "低": 2}
        order_w = {"观察中": 0, "待决策": 1, "进行中": 2, "已完结": 3}
        def _meta(fname):
            m = {"创建": "", "状态": "进行中", "优先级": "中"}
            try:
                with open(os.path.join(TODO_DIR, fname), encoding="utf-8") as fh:
                    for line in fh.read().splitlines()[:15]:
                        mm = re.match(r"^- \*\*创建\*\*[:：]\s*(\d{4}-\d{2}-\d{2})", line)
                        if mm: m["创建"] = mm.group(1).strip()
                        mm = re.match(r"^- \*\*状态\*\*[:：]\s*(.+)$", line)
                        if mm: m["状态"] = mm.group(1).strip()
                        mm = re.match(r"^- \*\*优先级\*\*[:：]\s*(.+)$", line)
                        if mm: m["优先级"] = mm.group(1).strip()
            except Exception:
                pass
            return m
        files.sort(key=lambda f: (
            pri_w.get(_meta(f)["优先级"], 3),
            -_date_int(_meta(f)["创建"]),
            order_w.get(_meta(f)["状态"], 3),
        ))
        if 1 <= idx <= len(files):
            return os.path.join(TODO_DIR, files[idx - 1])
        return None
    # 标题模式：模糊匹配文件名（去 .md、去空格）
    t = target.replace(" ", "").replace(".md", "")
    for f in sorted(os.listdir(TODO_DIR)):
        if not f.endswith(".md"):
            continue
        base = f[:-3].replace(" ", "")
        if t in base or base in t:
            return os.path.join(TODO_DIR, f)
    return None

def check_links(content):
    """扫描 ## 关联 区段里的路径，检查是否存在。返回警告列表。"""
    warns = []
    in_assoc = False
    for line in content.splitlines():
        if line.startswith("## 关联"):
            in_assoc = True
            continue
        if in_assoc and line.startswith("## "):
            break
        if not in_assoc or not line.strip().startswith("- "):
            continue
        # 提取路径（去掉 技能/脚本/文件/会话 等前缀词；中文括号/空格/标点视为路径终止）
        m = re.search(r"(/opt/data/[^\s,，（）()]+|/tmp/[^\s,，（）()]+|~/[^\s,，（）()]+)", line)
        if m and not m.group(1).startswith("/opt/data/待办"):
            p = m.group(1).replace("~", os.path.expanduser("~"))
            if not os.path.exists(p):
                warns.append(f"{p}（不存在，死引用）")
    return warns

def main():
    if len(sys.argv) < 2:
        print("❌ 需要: todo_finish.py <标题或序号> [--summary \"总结\"] [--clean-tmp] [--session <会话链接>]")
        return 1
    target = sys.argv[1]
    summary = None
    clean_tmp = False
    session_link = None
    rest = sys.argv[2:]
    i = 0
    while i < len(rest):
        if rest[i] == "--summary" and i + 1 < len(rest):
            summary = rest[i + 1]
            i += 2
        elif rest[i] == "--clean-tmp":
            clean_tmp = True
            i += 1
        elif rest[i] == "--session" and i + 1 < len(rest):
            session_link = rest[i + 1]
            i += 2
        else:
            i += 1

    path = find_file(target)
    if not path:
        print(f"❌ 找不到待办: {target}")
        return 1
    print(f"📌 目标: {os.path.basename(path)}")

    with open(path, encoding="utf-8") as fh:
        content = fh.read()

    # 1. 追加总结
    if not summary:
        print("（未提供 --summary，请在下面粘贴总结，空行+回车结束）")
        lines = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if not line.strip():
                break
            lines.append(line)
        summary = "\n".join(lines)
    if not summary.strip():
        print("⚠️ 无总结内容，跳过总结区段")
        summary = None

    if summary:
        today = datetime.now().strftime("%Y-%m-%d")
        session_note = f"\n- 会话回溯：{session_link}" if session_link else ""
        summary_block = f"\n## 总结（{today}）\n{summary}\n{session_note}\n" if session_link else f"\n## 总结（{today}）\n{summary}\n"
        if "\n## 总结" in content:
            # 已有总结区段则替换
            content = re.sub(r"\n## 总结.*", summary_block, content, flags=re.S)
        else:
            content = content.rstrip() + "\n" + summary_block

    # 2. 校验关联路径
    warns = check_links(content)
    if warns:
        print("⚠️ 关联路径校验：")
        for w in warns:
            print(f"  · {w}")

    # 3. 清理临时路径（仅 --clean-tmp 且路径在 关联 里标记）
    if clean_tmp:
        for line in content.splitlines():
            m = re.search(r"(/tmp/[^\s,，]+|/opt/data/tmp/[^\s,，]+)", line)
            if m and os.path.exists(m.group(1)):
                print(f"🗑️ 清理临时: {m.group(1)}")
                try:
                    if os.path.isdir(m.group(1)):
                        shutil.rmtree(m.group(1))
                    else:
                        os.remove(m.group(1))
                except Exception as e:
                    print(f"  ⚠️ 清理失败: {e}")

    # 4. 头部状态改已完结
    content = re.sub(
        r"^- \*\*状态\*\*[:：].*$",
        "- **状态**：已完结",
        content,
        count=1,
        flags=re.M,
    )

    # 移入已完成/
    os.makedirs(DONE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    dest = os.path.join(DONE_DIR, os.path.basename(path))
    shutil.move(path, dest)
    print(f"✅ 已完结并归档: {os.path.basename(path)} → 已完成/")
    print(f"   📄 留档: {dest}")

    # 建议记忆摘要（供主人确认，不自动写）
    if summary:
        first_line = summary.strip().splitlines()[0]
        print("\n📝 建议记忆摘要（供主人确认，不自动写入）：")
        print(f"  {first_line[:60]}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
