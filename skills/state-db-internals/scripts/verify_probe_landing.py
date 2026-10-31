#!/usr/bin/env python3
"""落库守卫探针：一次性取全证据并给出判定（state.db 只读取证脚本）。

用法：
    python3 verify_probe_landing.py <ts_ms> <标题片段> [--round N] [--db PATH]

例：
    python3 verify_probe_landing.py 1791123677089 首条消息落库守卫 --round 3

参数：
    ts_ms        探针消息里的毫秒时间戳（唯一权威定位键）
    标题片段      title/content 里出现的家族片段，用于 Step 0/1 与跨 session 全文检索
    --round N    可选：把 round 号加入 title 匹配，缩小范围
    --db PATH    可选：覆盖 $HERMES_HOME/state.db

输出：Step 0~3 证据块 + 对照实验 + id 空洞 + 一行 VERDICT。
判定分支的语义见 SKILL.md「判定优先级」；本脚本只是把那些手写 SQL 固化下来，
首次在新库/新列结构上使用时先看一遍输出是否与手查一致。
只读连接（mode=ro），不会写入或修改任何数据。
"""

import argparse
import os
import sqlite3
import sys
import time
import traceback

TEXT_COLUMNS = [
    "content",
    "tool_calls",
    "api_content",
    "reasoning",
    "reasoning_content",
    "reasoning_details",
]


def resolve_db(explicit=None):
    """先定位真实 state.db——绝不断言 ~/.hermes 或 $HOME/.hermes。"""
    if explicit:
        return explicit
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    cand = os.path.join(home, "state.db")
    if os.path.exists(cand):
        return cand
    for base in (home, "/home/agent", "/opt/data", os.path.expanduser("~")):
        cand = os.path.join(base, ".hermes", "state.db")
        if os.path.exists(cand):
            return cand
    return None


def open_ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ts_ms", type=int)
    ap.add_argument("title_part")
    ap.add_argument("--round", default=None)
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    ts_ms = args.ts_ms
    ts = ts_ms / 1000.0  # 单位陷阱：库里是秒，探针给的是毫秒
    now = time.time()  # 本脚本运行起点：此后落库的行都是「本轮自产」
    part = args.title_part
    rnd = args.round

    path = resolve_db(args.db)
    if not path:
        print("VERDICT: 找不到 state.db（先按 SKILL.md「数据库位置」定位）")
        return 2
    conn = open_ro(path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    def q(sql, *a):
        cur.execute(sql, a)
        return cur.fetchall()

    def head(t):
        print("\n===== %s =====" % t)

    head("环境")
    print("db=%s  ts_ms=%d  ts=%.3f  now-ts=%.1fs" % (path, ts_ms, ts, now - ts))

    # ---------- Step 0：精确 title ----------
    head("Step 0 精确 title")
    exact_titles = ["%s round %s %d" % (part, rnd, ts_ms)] if rnd else []
    exact_titles.append("%s %d" % (part, ts_ms))
    hit0 = []
    for t in exact_titles:
        rows = q(
            "SELECT id,source,started_at,message_count,title FROM sessions WHERE title=?",
            t,
        )
        print("  title=%r -> %d 行" % (t, len(rows)))
        for r in rows:
            print("   ", tuple(r))
        hit0 += rows
    if not hit0:
        print("  0 行不阻断：探针 title 常带 [probe:xxx] 前缀或干脆不落 title，继续 Step 1/2")

    # ---------- Step 1：家族 + round ----------
    head("Step 1 家族+round 定位")
    if rnd:
        rows1 = q(
            "SELECT id,source,started_at,message_count,title FROM sessions "
            "WHERE title LIKE ? AND title LIKE ? ORDER BY started_at DESC LIMIT 12",
            "%" + part + "%",
            "%round " + str(rnd) + "%",
        )
    else:
        rows1 = q(
            "SELECT id,source,started_at,message_count,title FROM sessions "
            "WHERE title LIKE ? ORDER BY started_at DESC LIMIT 12",
            "%" + part + "%",
        )
    for r in rows1:
        print("  ", tuple(r))
    print("  注意：按 float started_at 排序看时间序；id 字符串序≠时间序")

    # ---------- Step 2：窗口逐级放宽 ----------
    windows = {}
    for w in (5, 60, 300):
        rows = q(
            "SELECT id,source,started_at,message_count,title FROM sessions "
            "WHERE started_at BETWEEN ? AND ? ORDER BY started_at",
            ts - w,
            ts + w,
        )
        windows[w] = rows
        head("Step 2 窗口 ±%ds" % w)
        for r in rows:
            print("   %+.1fs" % (r["started_at"] - ts), tuple(r))

    # ---------- 锚点：标题精确命中优先，其次累积型；残桩不作锚点 ----------
    head("锚点选取（累积型优先，残桩不作锚点；同批多 round 时按标题+时距选）")

    def _round_match(title):
        return bool(title) and part in title and (rnd is None or ("round " + str(rnd)) in title)

    def _fam_match(title):
        return bool(title) and part in title

    cand = [r for r in windows[300] if (r["message_count"] or 0) > 1]
    # ⚠️ 不得按 message_count 降序选锚点：同批多个 round 常同窗落库（round 1~8 一起跑），
    # 越靠后的 round 累积轮次越多、mc 越大，会把 mc 更大的邻轮抢成锚点，
    # 导致「锚点取证/漂移复核」查的不是本批、而同家族 user 行查询查的却是本批——三段口径打架。
    # 正确口径：① 标题精确命中「家族+本 round」优先 ② 其次仅同家族 ③ 各级内按 started_at 最靠近目标时刻。
    pool = sorted(
        cand,
        key=lambda r: (
            0 if _round_match(r["title"]) else (1 if _fam_match(r["title"]) else 2),
            abs((r["started_at"] or 0) - ts),
        ),
    )
    anchor = pool[0] if pool else None
    for r in pool[:6]:
        kind = "本round" if _round_match(r["title"]) else ("同家族" if _fam_match(r["title"]) else "邻窗")
        print(
            "  候选[%s] mc=%s %+.1fs %s"
            % (kind, r["message_count"], (r["started_at"] or 0) - ts, tuple(r))
        )
    if anchor is None:
        print("  ±300s 内无累积型 session 候选")
    else:
        print("  → 锚点", tuple(anchor))
        if not _round_match(anchor["title"]):
            print("  ⚠️ 锚点标题未精确命中本 round（%r），核对是否同批多轮抢锚" % anchor["title"])

    # ---------- 命中即取证：首查一次取全 ----------
    first_snapshot = None
    anchor_facts = {}
    if anchor is not None:
        sid = anchor["id"]
        head("锚点首查取证 %s" % sid)
        meta = q(
            "SELECT id,source,title,started_at,message_count,tool_call_count,end_reason,profile_name "
            "FROM sessions WHERE id=?",
            sid,
        )[0]
        first_snapshot = dict(meta)
        print("  meta:", dict(meta))
        roles = q(
            "SELECT role,count(*) FROM messages WHERE session_id=? GROUP BY role", sid
        )
        print("  roles:", [(r[0], r[1]) for r in roles])
        msgs = q(
            "SELECT id,role,timestamp,length(content),length(tool_calls) FROM messages "
            "WHERE session_id=? ORDER BY id",
            sid,
        )
        print("  消息 %d 条" % len(msgs))
        for r in msgs:
            print("   ", tuple(r))
        ids = [r["id"] for r in msgs]
        if ids:
            gaps = [i for i in range(ids[0], ids[-1] + 1) if i not in set(ids)]
            print("  session 内 id 空洞 %d 个" % len(gaps))
        anchor_facts = {
            "meta": dict(meta),
            "roles": {r[0]: r[1] for r in roles},
            "user_preview": q(
                "SELECT id,timestamp,substr(content,1,160) FROM messages "
                "WHERE session_id=? AND role='user' ORDER BY id LIMIT 3",
                sid,
            ),
        }
        for r in anchor_facts["user_preview"]:
            print("  首条 user 行:", tuple(r))

    # ---------- 整批复核：同窗口同家族全部 session ----------
    head("整批复核（±60s 内每条 session 的现状 + 各自 user 行数）")
    for r in windows[60]:
        uc = q(
            "SELECT count(*) FROM messages WHERE session_id=? AND role='user'", r["id"]
        )[0][0]
        print("  %s src=%-10s mc=%-3s user=%-3s %s" % (r["id"], r["source"], r["message_count"], uc, r["title"]))

    # ---------- 跨 session 逐列扫探针 ms（剔除本轮自产行）----------
    head("跨 session 逐列扫 ms 字面量（已剔除本轮自产 timestamp>=now 的行）")
    ms_hits = {}
    cols = [c for c in TEXT_COLUMNS]
    have = {r[1] for r in q("PRAGMA table_info(messages)")}
    cols = [c for c in cols if c in have]
    for col in cols:
        rows = q(
            "SELECT id,session_id,role,timestamp FROM messages "
            "WHERE %s LIKE ? AND timestamp < ? ORDER BY id" % col,
            "%" + str(ts_ms) + "%",
            now,
        )
        ms_hits[col] = rows
        print("  %-18s %d 行 %s" % (col, len(rows), [tuple(r) for r in rows[:4]]))

    head("跨 session 找同家族 user 行（归属错位判据）")
    # ⚠️ rnd 是 str（argparse 未转 int），拼 SQL 参数必须 str(rnd)；
    # 且必须用 ts_ms 收敛——同家族同 round 的更早批次（换天重发）不是本批，
    # 仅靠 'round N' 匹配会把旧批次的 user 行算成本批证据。
    if rnd:
        fam_user = q(
            "SELECT id,session_id,timestamp,substr(content,1,90) FROM messages "
            "WHERE role='user' AND content LIKE ? AND content LIKE ? AND content LIKE ? ORDER BY id",
            "%" + part + "%",
            "%round " + str(rnd) + "%",
            "%" + str(ts_ms) + "%",
        )
        fam_user_early = q(
            "SELECT id,session_id,timestamp,substr(content,1,90) FROM messages "
            "WHERE role='user' AND content LIKE ? AND content LIKE ? AND content NOT LIKE ? ORDER BY id",
            "%" + part + "%",
            "%round " + str(rnd) + "%",
            "%" + str(ts_ms) + "%",
        )
    else:
        fam_user = q(
            "SELECT id,session_id,timestamp,substr(content,1,90) FROM messages "
            "WHERE role='user' AND content LIKE ? AND content LIKE ? ORDER BY id",
            "%" + part + "%",
            "%" + str(ts_ms) + "%",
        )
        fam_user_early = []
    for r in fam_user:
        print("  ", tuple(r))
    if not fam_user:
        print("  0 行")
    if fam_user_early:
        print("  ⚠️ 同 round 但 ms 不同的旧批次 user 行 %d 条（不作本批证据）：" % len(fam_user_early))
        for r in fam_user_early[:5]:
            print("    ", tuple(r))

    # ---------- 对照实验：窗口内全库 user 行 ----------
    head("对照实验（同窗口全库 role=user 行，与本探针无关的行也算）")
    win_user = q(
        "SELECT id,session_id,timestamp,substr(replace(coalesce(content,''),char(10),' '),1,90) "
        "FROM messages WHERE role='user' AND timestamp BETWEEN ? AND ? ORDER BY id",
        ts - 60,
        ts + 300,
    )
    print("  窗口(-60s~+300s) user 行 = %d" % len(win_user))
    for r in win_user[:10]:
        print("   ", tuple(r))
    last_user = q(
        "SELECT id,session_id,timestamp,substr(replace(coalesce(content,''),char(10),' '),1,80) "
        "FROM messages WHERE role='user' ORDER BY id DESC LIMIT 1"
    )
    print("  全库最后一条 user 行:", tuple(last_user[0]) if last_user else None)
    if last_user:
        print("  它距目标时刻 %+.1fs（>0 表示探针时刻之后全库再无 user 行）" % (last_user[0]["timestamp"] - ts))

    # ---------- id 空洞（frontier 法）----------
    head("messages.id 空洞（frontier 法：区分「没写过」与「写了被 DELETE」）")
    before = q("SELECT max(id) FROM messages WHERE timestamp < ?", ts)
    after = q("SELECT min(id) FROM messages WHERE timestamp >= ?", ts)
    b = before[0][0]
    a = after[0][0]
    if b is None or a is None or a <= b + 1:
        print("  无空洞可判（b=%s a=%s）" % (b, a))
    else:
        exist = {r[0] for r in q("SELECT id FROM messages WHERE id BETWEEN ? AND ?", b + 1, a)}
        miss = [i for i in range(b + 1, a) if i not in exist]
        print("  frontier b=%d a=%d 空洞 %d 个" % (b, a, len(miss)))
        if miss:
            print("  空洞区间: %d..%d（前10: %s）" % (miss[0], miss[-1], miss[:10]))

    # ---------- 漂移复核：与首查逐字段比对 ----------
    head("锚点漂移复核（覆盖复用检测）")
    drift = []
    if anchor is not None and first_snapshot is not None:
        now_meta = q(
            "SELECT id,source,title,started_at,message_count,tool_call_count,end_reason,profile_name "
            "FROM sessions WHERE id=?",
            anchor["id"],
        )
        if not now_meta:
            print("  session 行已消失 → 原始记录被删")
            drift.append("disappeared")
        else:
            nm = dict(now_meta[0])
            for k in first_snapshot:
                if nm.get(k) != first_snapshot.get(k):
                    drift.append("%s: %r -> %r" % (k, first_snapshot.get(k), nm.get(k)))
            print("  首查:", first_snapshot)
            print("  复查:", nm)
            print("  漂移字段:", drift or "无")
    else:
        print("  无锚点可复核")

    # ---------- 判定 ----------
    head("VERDICT")
    print("判定优先级（自上而下先命中者生效）：")
    print(" 1. user 行≥1（锚点内或跨 session 命中）        → 已落库")
    print(" 2. 锚点漂移/消失                                → 覆盖复用：原始行已抹除/删除（非写入失败）")
    print(" 3. 锚点 source∈(tui,NULL) 且 title IS NULL 且无漂移 → TUI 已知盲区，非失败（窗口 user=0 是常态）")
    print(" 4. 窗口内全库 user 行=0（且锚点非 TUI 盲区）      → 非归属错位；探针 user 行从未独立留存")
    print(" 5. ±300s 内无累积型 session                      → 请求未送达 / 网关丢弃")
    print("")
    a_src = anchor_facts.get("meta", {}).get("source") if anchor_facts else None
    a_title = anchor_facts.get("meta", {}).get("title") if anchor_facts else None
    n_user = (anchor_facts.get("roles") or {}).get("user", 0)
    fam_user_n = len(fam_user)
    if n_user >= 1 or fam_user_n >= 1:
        print("VERDICT: 已落库（锚点 user 行=%d，跨 session 同家族 user 行=%d）" % (n_user, fam_user_n))
    elif drift or (anchor is not None and not q("SELECT 1 FROM sessions WHERE id=?", anchor["id"])):
        print("VERDICT: 覆盖复用致原始记录已抹除/删除（非写入失败、非 TUI 盲区、非归属错位）")
        for d in drift:
            print("  漂移:", d)
    elif a_src in ("tui", None) and a_title is None:
        print("VERDICT: TUI 通道已知盲区（非写入失败；user 首条并入 assistant 首轮，窗口 user=0 属常态）")
    elif len(win_user) == 0:
        print("VERDICT: 窗口内全库无任何 user 行（锚点非 TUI 盲区）→ 探针首条 user 从未独立留存；"
              "非归属错位；若有 id 空洞/整批复用佐证则为原始行被删除")
    elif anchor is None:
        print("VERDICT: ±300s 内无累积型 session → 请求未送达 / 网关丢弃（查 gateway.log）")
    else:
        print("VERDICT: session 已建但 user 行=0（source=%s）→ 首条 user 未写入，查压缩/rewind" % a_src)
    if anchor is not None and not _round_match(anchor["title"]):
        print("⚠️ 锚点标题未精确命中本 round（%r）——VERDICT 仅供参考，先按 SKILL.md「三级匹配仍会选错」条重锚（认领当前验证会话 / ms 最早非自产行）再采信" % anchor["title"])
    conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
