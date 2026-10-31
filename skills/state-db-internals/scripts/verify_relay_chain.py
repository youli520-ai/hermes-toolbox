#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E2E 接力链（UI-E2E-SUMMARY-MARK）全链一致性核对——只读。

用法：
  python3 verify_relay_chain.py <B1> [B2 ... Bn] [--a <A id>] [--db PATH]

- B1..Bn = 各周期 B 会话 id（title 形如「接力UI源-NNNNNN-b」），按 started_at 升序给全，
  最后一个是当前会话——**当前会话也在链上，别剔除**。
- --a 给本周期 A（一次性源）会话 id；不传则跳过 A 注销状态检查。

判据（与 relay-handoff-e2e.md 取证三步一致）：
  1. 各 B 首条 active user 行 = 交接摘要（【交接摘要】开头 + 三要素齐全）
  2. 各 B 首条 user 行提取 ## 目标 字段做 sha256 全链比对（零丢字判定）
  3. 原话标记（UI-E2E-原话-NNNNNN）逐轮互异（重复 = 串轮）
  4. display_kind 均 None（摘要走普通用户消息通道，不触发灰条）
  5. A 注销两阶段状态查询（messages 行 / sessions 行，任一阶段存留均属预期）
  6. 灰条样本判定：存量分类统计 + 唯一判据 hidden ∧ _compressed_summary=1（0 即「无生产实证」）

输出逐行打印、可直接抄进收口报告；任一前置判据不满足即退出码 2 并指出问题 id。
"""
import argparse
import hashlib
import re
import sqlite3
import sys


def main():
    ap = argparse.ArgumentParser(description="E2E 接力链全链一致性核对（只读）")
    ap.add_argument("sessions", nargs="+",
                    help="B 会话 id 链，按 started_at 升序，末位为当前会话")
    ap.add_argument("--a", dest="a_id", default=None,
                    help="本周期 A（一次性源）会话 id，可选")
    ap.add_argument("--db", default="/home/agent/.hermes/state.db",
                    help="state.db 路径（默认 default profile 实测位置）")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    cur = con.cursor()

    marks, hashes = [], []
    print("原话标记 | 首条user字节 | display_kind | 目标sha256前12 | 摘要三头齐全")
    for sid in args.sessions:
        cur.execute(
            "SELECT content, display_kind, length(content) FROM messages "
            "WHERE session_id=? AND role='user' AND active=1 ORDER BY id LIMIT 1",
            (sid,))
        row = cur.fetchone()
        if row is None:
            print("%s | 无 active user 行——先核对该 id 是否本周期 B 会话" % sid)
            sys.exit(2)
        content, display_kind, blen = row
        m = re.search(r"## 目标\n(.+?)\n", content)
        if not m:
            print("%s | 首条 user 行无 ## 目标 字段——疑混入 A 会话或非摘要行" % sid)
            sys.exit(2)
        mk = re.search(r"UI-E2E-原话-(\d+)", content)
        if not mk:
            print("%s | 缺原话标记，三要素不全" % sid)
            sys.exit(2)
        h = hashlib.sha256(m.group(1).encode()).hexdigest()[:12]
        marks.append(mk.group(1))
        hashes.append(h)
        print("原话-%s | len=%d | %s | %s | %s" % (
            mk.group(1), blen, display_kind, h,
            content.strip().startswith("【交接摘要】")))
    print("## 目标 sha256 全同:", len(set(hashes)) == 1)
    print("原话标记互异:", len(set(marks)) == len(marks), marks)

    if args.a_id:
        cur.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (args.a_id,))
        nmsg = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM sessions WHERE id=?", (args.a_id,))
        nses = cur.fetchone()[0]
        print("A(%s): messages=%d sessions=%d —— 两阶段清理中；0/0 为终态，任一阶段存留均属预期"
              % (args.a_id, nmsg, nses))

    # 灰条证据：先按语义分类，再决定能不能下结论
    # ⚠️ 行数 ≠ 语义类别：hidden 主体是打断占位；压缩摘要件 display_kind=None（merge-into-tail）；
    #    只有 standalone 交接摘要才会被 _summary_display_kind 置 'hidden'。
    def count(sql, params=()):
        cur.execute(sql, params)
        return cur.fetchone()[0]

    print("—— 存量统计（active=1 口径；跨轮只同口径可比，仅存量、不作灰条证据）——")
    print("压缩摘要件:", count("SELECT COUNT(*) FROM messages WHERE content LIKE '%[CONTEXT COMPACTION — REFERENCE ONLY]%' AND active=1"))
    print("_compressed_summary=1:", count("SELECT COUNT(*) FROM messages WHERE _compressed_summary=1 AND active=1"))
    print("display_kind='hidden':", count("SELECT COUNT(*) FROM messages WHERE display_kind='hidden' AND active=1"))

    print("—— 灰条样本判定（唯一判据：hidden ∧ _compressed_summary=1）——")
    n_bar = count("SELECT COUNT(*) FROM messages WHERE display_kind='hidden' AND _compressed_summary=1")
    kinds = {}
    for (ac,) in cur.execute("SELECT COALESCE(api_content,'') FROM messages WHERE display_kind='hidden' AND active=1"):
        head = ac.strip()[:32]
        k = ('打断占位' if 'interrupt' in head
             else '空内容' if not head else '其它:' + head[:20])
        kinds[k] = kinds.get(k, 0) + 1
    print("hidden 构成:", kinds if kinds else "无")
    print("standalone 交接摘要样本(灰条):", n_bar,
          "→ " + ("有生产样本，可作灰条实证" if n_bar
                  else "零样本：灰条无生产实证，UI 项结论只能写「未验证/能力边界」"))
    con.close()


if __name__ == "__main__":
    main()
