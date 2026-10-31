# -*- coding: utf-8 -*-
"""env-reminder —— 环境便签插件（星光，2026-09-05 建；0.3.0 迁移 pre_llm_call）。

读 /opt/data/state/env_delta.json（由 cron 每 5 分钟采集生成），存在新鲜环境变化时把便签
**注入当轮用户消息**——不碰 system prompt、只在变化时注入。

0.3.0（2026-09-21）架构迁移：注入点 pre_gateway_dispatch → pre_llm_call
  - 起因（多 profile 发现收不到 → 深挖）：网关钩子只覆盖网关入站（飞书/微信/A2A），
    TUI / 桌面 / 6060 API 都是**进程内直跑 AIAgent**（cli.py、tui_gateway、api_server 自带
    agent 生命周期），永远不经过 run_inbound → 便签永远收不到。
  - pre_llm_call 在 agent 每轮触发（agent/turn_context.py::build_turn_context，由
    agent/conversation_loop.py 调用），**所有入口共用同一条 agent 循环** → 一处代码全入口覆盖。
  - 注入形式：返回 {"context": 便签} → 框架以 api_content sidecar 注入当轮用户消息的 API 副本
    （durable 会话行仍是干净内容、不污染历史；下轮 replay 前缀稳定）。
  - 附带收益：不再与 fast-command 的 rewrite/skip 竞态（快路径 skip 后 agent 不跑，本钩子自然
    不触发），原 0.2.x 的 _would_fast_path 预判逻辑删除。
  - 子代理跳过：parent_session_id 非空 = 子代理轮次，不注入（便签是给用户面对的主 agent 的）。

白名单（0.3.0 判据改造：pre_llm_call 没有 chat_type/chat_id，改看 platform + sender_id + session_id）
  - 本机入口（local_platforms，默认 tui/cli/desktop/local/api_server）→ 放行（能摸到本机即越过物理边界）
  - 其它平台（网关类 feishu/weixin/a2a/…）→ sender_id 需命中 owner_user_id 或 allowed_senders；
    且 dm_only=true 时，从 session_id（形如 agent:<profile>:<platform>:<chat_type>:<id>）解析出的
    chat_type 必须是 dm，解析不出 → 拒绝（fail-closed，防群聊泄露）
  - 钩子异常 → return None（fail-open，绝不影响对话）

监控项（5 项）：主机在线/离线、示例服务就绪/不可用、磁盘跨 90% 阈值、服务探针、技能审查 pending（示例，键与采集脚本对应）
"""
import json
import os
import re
import time
from datetime import datetime

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PLUGIN_DIR, "config.json")

DEFAULT_LOCAL_PLATFORMS = ["tui", "cli", "desktop", "local", "api_server"]

_logger = None


def _log(msg, *args):
    global _logger
    if _logger is None:
        import logging
        _logger = logging.getLogger("env-reminder")
    if args:
        msg = msg % args
    _logger.info("[env-reminder] %s", msg)


def _load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_GW_KEY_RE = re.compile(r"^agent:[^:]+:[^:]+:([^:]+):")


def _parse_chat_type(session_id):
    """从网关会话键 agent:<profile>:<platform>:<chat_type>:<id> 解析 chat_type；解析不出返回 ""。"""
    m = _GW_KEY_RE.match(session_id or "")
    return m.group(1) if m else ""


def _authorized(platform, sender_id, session_id, cfg=None):
    """白名单：本机入口放行；网关平台按 sender + dm_only 判定。"""
    cfg = cfg or _load_config()
    if not cfg.get("enabled", True):
        return False
    plat = (platform or "").strip().lower()
    locals_ = cfg.get("local_platforms", DEFAULT_LOCAL_PLATFORMS) or []
    if plat in {str(p).strip().lower() for p in locals_}:
        return True
    allowed = {s for s in (cfg.get("allowed_senders") or []) if s}
    owner = cfg.get("owner_user_id")
    if owner:
        allowed.add(owner)
    if not sender_id or sender_id not in allowed:
        return False
    if cfg.get("dm_only", True) and _parse_chat_type(session_id) != "dm":
        return False
    return True


def _load_delta():
    """读增量文件：不存在 / 过期（>max_age_min）/ 无 items → None"""
    cfg = _load_config()
    path = cfg.get("delta_path", "/opt/data/state/env_delta.json")
    try:
        mtime = os.path.getmtime(path)
        if time.time() - mtime > cfg.get("max_age_min", 12) * 60:
            return None
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if not d.get("items"):
            return None
        return d
    except Exception:
        return None


# 监控项展示名与状态值中文化（与采集脚本 collect.py 的 key 对应）
_LABELS = {
    "host.online": "主机A",
    "service.demo": "示例服务",
    "service.api": "服务探针",
    "skill.review": "技能审查",
}
_VALUE_CN = {
    "online": "在线", "offline": "离线",
    "up": "正常", "down": "不可用",
}
# 按监控项覆盖状态值文案（ComfyUI 用「就绪」、HA 用「正常」）
_VALUE_CN_PER_KEY = {
    "service.demo": {"up": "就绪", "down": "不可用"},
}
# 第 6 项 memory.<profile> 的 profile 中文名（采集端 collect.MEMORY_HOMES 的 key）
_PROFILE_CN = {
    "default": "默认",
    "profile-a": "分身A",
    "profile-b": "分身B",
}


def _fmt_reminder(delta):
    """把 delta 格式化为便签文本（简短，≤max_len）。
    3.1：item 带 action → 行尾附「→ 建议…」；3.2：warn 持续项前置 ◉，critical 前置 ⚠️。
    P0（2026-09-06）：按 critical > seq（事件型0/持续型1）> 原顺序 排序；
    截断时优先保 seq=0 事件型，持续型（seq=1）塞不下时优先丢弃，避免关键翻转被持续项淹没。"""
    cfg = _load_config()
    lines = ["[环境便签 %s]" % delta.get("ts", "")]
    items = delta.get("items", [])
    # critical 置顶（目前无实例，预留给后续 95%+ 磁盘）→ seq 事件型优先 → 原顺序
    items_sorted = sorted(
        items,
        key=lambda it: (
            0 if it.get("level") == "critical" else 1,
            it.get("seq", 1),
            0,
        ),
    )
    rendered = []
    for it in items_sorted:
        k = it.get("k", "")
        f_v = it.get("from", "")
        t_v = it.get("to", "")
        note = it.get("note", "")
        action = it.get("action", "")
        level = it.get("level", "info")
        prefix = "⚠️" if level == "critical" else ("◉" if level == "warn" else "-")
        if k == "skill.review":
            # 持续型提醒：技能变更待审查。清单可能很长（20+ 技能名），
            # 必须限长——否则整行超 max_len 会拖垮便签（2026-09-29 实测：23 个技能名
            # 拼出 429 字符行 > max_len 400 → 整条便签只剩标题、静默不注入）。
            # 只显示前 3 个 + 余数，完整清单看 env_state.json。
            names = [n.strip() for n in (note or "").split(",") if n.strip()]
            shown = "、".join(names[:3])
            more = "等 %d 个" % len(names) if len(names) > 3 else ""
            rendered.append("%s 技能审查：%s 个待审查（%s%s）" % (prefix, t_v, shown, more))
            continue
        if k.startswith("memory."):
            # 记忆 L0 占用（第 6 项，持续型）：值是百分比，profile 名中文化
            pname = k[len("memory."):]
            name = "记忆·" + _PROFILE_CN.get(pname, pname)
            if f_v in ("越限", "偏高") and note in ("越限", "偏高"):
                # 持续提醒态：同 disk，不重复画箭头
                line = "%s %s：%s（%s）" % (prefix, name, t_v, note)
            else:
                line = "%s %s：%s → %s" % (prefix, name, f_v, t_v)
                if note:
                    line += "（%s）" % note
            if action:
                line += " → " + action
            rendered.append(line)
            continue
        if k.startswith("disk."):
            name = "磁盘 " + k[len("disk."):]
            if f_v == "越限" and note == "越限":
                # 持续越限态：不再重复画箭头
                line = "%s %s：%s（%s）" % (prefix, name, t_v, note)
            else:
                line = "%s %s：%s → %s" % (prefix, name, f_v, t_v)
                if note:
                    line += "（%s）" % note
            if action:
                line += " → " + action
        else:
            name = _LABELS.get(k, k)
            vmap = _VALUE_CN_PER_KEY.get(k, _VALUE_CN)
            line = "%s %s：%s → %s" % (prefix, name, vmap.get(f_v, f_v), vmap.get(t_v, t_v))
        rendered.append(line)
    # 截断保 seq=0：先拼事件型（seq=0），持续型（seq=1）塞不下就丢
    # （事件型错过就错过，持续型下轮还在）
    max_len = cfg.get("max_len", 400)
    text = lines[0]

    # 独立截断函数：单行超长也保留开头，防止某行超长撑爆整体
    def _clip(s, budget):
        return s if len(s) <= budget else s[:budget] + "…"

    for it, line in zip(items_sorted, rendered):
        seq = it.get("seq", 1)
        budget = max_len - len(text) - 1
        if budget <= 0:
            break
        cand = _clip(line, budget)
        if len(cand) > budget:
            # 单行超长（clip 都救不回）：丢弃这一行，继续尝试后面的行。
            # 2026-09-29 修：原为 break → 一条超长持续型便签会连坐杀掉其后所有
            # 事件型告警（如「主机掉线」）。丢弃本行损失可控，吞掉告警不可接受。
            continue
        if seq == 1 and len(text) + 1 + len(cand) > max_len:
            # 持续型放不下：丢弃（保留已拼的事件型）
            continue
        text += "\n" + cand
    # 兜底：若连事件型都挤不下（极少数），仍截断式保留开头（防完全空便签）
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


def on_pre_llm_call(session_id=None, task_id=None, turn_id=None, user_message=None,
                    conversation_history=None, is_first_turn=False, model=None,
                    platform=None, parent_session_id=None, sender_id=None, **kwargs):
    """pre_llm_call：有新鲜环境变化且入口已授权 → 返回 {"context": 便签} 注入当轮用户消息。

    覆盖全部入口（TUI/桌面/6060 API/飞书/微信/A2A），因为本钩子在 agent 每轮触发。
    子代理轮次（parent_session_id 非空）跳过；任何异常 fail-open 返回 None。
    """
    try:
        if parent_session_id:
            return None
        cfg = _load_config()
        if not _authorized(platform, sender_id, session_id, cfg):
            # 首轮且带身份信息才记一行，便于排查入口/白名单漂移（平时静默）
            if is_first_turn and (sender_id or platform):
                _log("跳过注入（未授权）：platform=%s sender=%s session=%s",
                     platform, sender_id, session_id)
            return None
        delta = _load_delta()
        if not delta:
            return None
        reminder = _fmt_reminder(delta)
        if not reminder:
            return None
        if is_first_turn:
            # 每会话一行入口诊断：确认各入口的 platform/sender 实际取值
            _log("首轮诊断：platform=%s sender=%s session=%s → 授权通过",
                 platform, sender_id, session_id)
        _log("注入环境便签 ts=%s items=%d platform=%s",
             delta.get("ts", ""), len(delta.get("items", [])), platform)
        return {"context": reminder}
    except Exception as e:
        # 异常 -> 不影响对话（fail-open）
        _log("钩子异常（fail-open 不影响对话）: %s", e)
        return None


def register(ctx):
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
