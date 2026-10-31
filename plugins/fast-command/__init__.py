# -*- coding: utf-8 -*-
"""fast-command —— 智能家居确定性指令快路径插件（星光）。

在网关把消息交给 agent（LLM）之前，用 pre_gateway_dispatch 钩子做指令库硬匹配：
  - 命中指令库精确/参数化口令，或设备中文名唯一解析（开/关/状态/查）
    且不在高危黑名单 → 直接后台执行 ha-ctl，回一行确认，然后 skip（0 token）。
  - 未命中 / 歧义 / 高危 / 非授权频道 → 返回 None，正常交给 agent 处理（fail-open）。

安全设计（主人红线）：
  - 只处理主人授权频道（DM），绝不越权控设备。
  - 高危指令（关机/重启/删除等）一律放行回 agent 二次确认，不走快路径。
  - ask 型 / script 型（报告/登录流）不放行，避免误吞澄清与特殊流程。
  - 钩子回调本身抛异常时，网关自动回退到正常派发（fail-open）。
  - 执行放后台线程 + 子进程隔离（ha-ctl 内部 sys.exit 不伤网关主进程）。
  - 每次快路径执行写审计日志（数据完整 + 可回溯；session 库落库因会话未创建留待后续）。
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent
CONFIG_PATH = PLUGIN_DIR / "config.json"
LIB_FILE = os.environ.get("FAST_COMMAND_LIB_FILE", "")  # 公开版默认空=禁用指令库引擎；需配套指令库 JSON+模块时配置  # 指令库 JSON；需配套指令库模块
CFG_FILE = os.environ.get("FAST_COMMAND_HA_CFG", "/opt/data/ha_ctl_config.json")
HA_CTL = os.environ.get("FAST_COMMAND_HA_CTL", "/opt/data/ha-ctl.py")

# /opt/data 上的共享模块（纯标准库，import 无副作用；resolve_entity 不退出）
sys.path.insert(0, "/opt/data")

_logger = None


def _log(msg):
    global _logger
    if _logger is None:
        import logging
        _logger = logging.getLogger("fast-command")
    _logger.info("[fast-command] %s", msg)


# ---------- 配置与缓存 ----------

_state = {"config": None, "lib_mtime": None, "lib": None, "cfg_mtime": None, "cfg": None}


def _load_config():
    # 每次实时读文件（config 改动热生效，改高危名单无需重启网关；读取开销可忽略）
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            _state["config"] = json.load(f)
    except Exception:
        _state["config"] = _state["config"] or {}
    return _state["config"]


def _load_lib():
    if not LIB_FILE:
        _state["lib"] = _state["lib"] or {"version": 1, "commands": {}}
        return _state["lib"]
    try:
        m = os.path.getmtime(LIB_FILE)
        if _state["lib"] is None or m != _state["lib_mtime"]:
            import command_lib
            _state["lib"] = command_lib.load()
            _state["lib_mtime"] = m
    except Exception as e:
        _log(f"指令库加载失败: {e}")
        _state["lib"] = _state["lib"] or {"version": 1, "commands": {}}
    return _state["lib"]


def _load_cfg():
    try:
        m = os.path.getmtime(CFG_FILE)
        if _state["cfg"] is None or m != _state["cfg_mtime"]:
            with open(CFG_FILE, encoding="utf-8") as f:
                _state["cfg"] = json.load(f)
            _state["cfg_mtime"] = m
    except Exception as e:
        _log(f"ha 配置加载失败: {e}")
        _state["cfg"] = _state["cfg"] or {}
    return _state["cfg"]


# 引擎2 单字动作前缀（唯一解析设备名；复合动作/模糊交给 agent）
_DEVICE_ACTIONS = ("打开", "关闭", "开", "关", "状态", "查")
# 指令库中不放行快路径的类型
_BLOCK_TYPES = ("ask", "script")


def _authorized(source):
    """白名单：仅主人授权频道（DM）。"""
    cfg = _load_config()
    if not cfg.get("enabled", True):
        return False
    if not source:
        return False
    owner = cfg.get("owner_user_id")
    allowed = set(cfg.get("allowed_chats", []))
    if cfg.get("dm_only", True) and source.chat_type != "dm":
        return False
    if source.user_id and owner and source.user_id == owner:
        return True
    if source.chat_id and source.chat_id in allowed:
        return True
    return False


def _is_high_risk(name, cmd=None):
    cfg = _load_config()
    high = set(cfg.get("high_risk_commands", []))
    if name and name in high:
        return True
    rx = cfg.get("high_risk_regex", [])
    blob = name or ""
    if cmd:
        blob = json.dumps(cmd, ensure_ascii=False)
    for p in rx:
        if p and re.search(p, blob, re.I):
            return True
    return False


def _classify(text):
    """返回 (kind, args) 或 None。
    kind: 'lib' -> 子进程参数 = [HA_CTL, text]（整句直送指令库）
          'panel' -> 面板型 script（配置驱动：指令库条目带 "panel": true），_build_reply 整份回显
          'device' -> 子进程参数 = [HA_CTL, action, name]（动作+名称，命中引擎2）
    仅返回最确定的命中；ask/script/高危/歧义/自然语言一律 None（交给 agent）。
    """
    text = (text or "").strip()
    # 剥离环境便签（2026-09-05 修复）：env-reminder 会在用户消息尾部追加
    #   「[环境便签 MM-DD HH:MM]\n- ...」块，若不剥离会导致指令库精确匹配失败、
    #   把本应 0-token 的快路径指令唤醒 agent。便签非用户指令，匹配前截断。
    text = re.sub(r"\s*\[环境便签.*$", "", text, flags=re.S).strip()
    if not text or text.startswith("/"):
        return None

    # 引擎1：指令库精确/参数化命中（不含 ask/script/高危）
    # 2026-09-05 配置驱动化（主人定「更快更脚本化更模板化」）：
    #   script 型条目带 "panel": true 即视为「面板直发」——快路径执行 ha-ctl 整句，
    #   _build_reply 按 kind=='panel' 整份回显（不再维护 hardcode 名单，新增面板只改指令库 JSON）
    # 旧版 _ALLOW_SCRIPT_REPORTS 硬编码名单已废弃（被 panel 字段取代），历史面板条目均已加 panel:true
    try:
        import command_lib
        data = _load_lib()
        status, name, cmd, _a = command_lib.search_keyword(data, text)
        if status in ("exact", "param") and cmd:
            ctype = cmd.get("type")
            is_panel = (ctype == "script" and cmd.get("panel") is True)
            # 2026-10-18 主人定「全放行车控指令」：script 型默认拦截，但配置
            # fast_script_allow 白名单内的条目放行（车控等确定性短脚本）
            allow = set(_load_config().get("fast_script_allow", []) or [])
            is_allowed_script = (ctype == "script" and name in allow)
            if (ctype not in _BLOCK_TYPES or is_panel or is_allowed_script) and not _is_high_risk(name, cmd):
                return (("panel", [text]) if is_panel else ("lib", [text]))
    except Exception as e:
        _log(f"指令库匹配异常: {e}")

    # 引擎1.6：待办序号查询（2026-09-05 主人反馈「查询不唤醒 agent」）
    #   词表配置驱动（2026-09-05 主人「后续能自己发现优化吗」——固化责任前移）：
    #   前缀/序号标记/追问词全部来自 config.json 的 todo_query_words，
    #   加新说法（如「瞄一眼第2条」）只改 JSON，config 实时读、无需重启网关。
    #   形态 A：…第N条/号/项…（带 look 前缀可选）；形态 B：待办N / 看一下待办N
    try:
        _cfg = _load_config()
        _tw = _cfg.get("todo_query_words", {})
        _look = "|".join(re.escape(w) for w in _tw.get("look_prefixes", ["看", "看看", "看一下", "看下", "查", "查看", "查下"]))
        _marks = "|".join(re.escape(w) for w in _tw.get("num_markers", ["条", "号", "项"]))
        _asks = "|".join(re.escape(w) for w in _tw.get("ask_suffixes", ["待办", "状态", "进度", "咋样", "怎么样", "情况", "什么", "是什么"]))
        _subs = "|".join(re.escape(w) for w in _tw.get("ask_sub_suffixes", ["状态", "进度", "情况"]))
        _ask_grp = f"(?:{_asks})(?:\\s*(?:{_subs}))?"
        m_todo = re.match(rf"^(?:{_look})?\s*第?\s*(\d+)\s*(?:{_marks})(?:{_ask_grp})?$", text)
        # 形态 B：待办N / 看一下待办N / 待办第N条
        if not m_todo:
            m_todo = re.match(rf"^(?:{_look})?\s*待办\s*第?\s*(\d+)\s*(?:{_marks})?(?:{_ask_grp})?$", text)
        if m_todo:
            seq = int(m_todo.group(1))
            done = "已完成" in text or "完成" in text
            return ("todo-query", [str(seq), "done" if done else "active"])
    except Exception as e:
        _log(f"待办查询匹配异常: {e}")

    # 纯数字防护（2026-08-28）：PIN/验证码/金额等纯数字输入一律放行 agent，
    #   不可能是有效的设备调节指令（调节必带设备名），防被引擎1.5误当亮度值吞掉丢失
    if re.fullmatch(r"\d+", text):
        return None

    # 引擎1.5：通用调节短语（亮度/色温 相对+绝对值，2026-08-28）
    try:
        import ha_control as _hc2
        cfg = _load_cfg()
        parsed = _hc2.parse_adjust(text)
        if parsed:
            dev, _kind, _mode, _v = parsed
            if _is_high_risk(text) or _is_high_risk(dev):
                return None
            try:
                _hc2.resolve_entity(cfg, dev)
            except ValueError:
                # 设备歧义/找不到 -> 交给 agent（绝不猜）
                return None
            # 审计分类精确化（T 修复 2026-08-28）：preset/oscillate/风扇风速 独立标签，不混记 relative
            kind_label = {"preset": "preset", "oscillate": "oscillate",
                          "percentage": "fan-speed"}.get(parsed[1], "relative")
            return (kind_label, [text])
    except Exception as e:
        _log(f"通用调节匹配异常: {e}")

    # 引擎2：设备中文名唯一解析（单字动作前缀 + 唯一命中）
    try:
        import ha_control
        cfg = _load_cfg()
        for act in _DEVICE_ACTIONS:
            if text.startswith(act) and len(text) > len(act):
                dname = text[len(act):]
                try:
                    ha_control.resolve_entity(cfg, dname)
                    # 高危兜底：设备路径也必须过高危检查（防引擎1拦后漏入引擎2）
                    if _is_high_risk(text) or _is_high_risk(dname):
                        return None
                    return ("device", [act, dname])
                except ValueError:
                    # 歧义 / 找不到 -> 交给 agent（绝不猜）
                    return None
    except Exception as e:
        _log(f"设备解析异常: {e}")

    return None


# ---------- 回复 / 审计 ----------

def _extract_reply(out):
    """从 ha-ctl 输出解析用户可见回复：OK:名称|...|回复（回复恒为第3段）。"""
    for line in (out or "").splitlines():
        if line.startswith("OK:"):
            parts = line.split("|")
            if len(parts) >= 3:
                r = parts[2].strip()
                if r:
                    return r
    return ""


def _audit(text, kind, rc, reply):
    try:
        cfg = _load_config()
        path = cfg.get("audit_log", "/opt/data/logs/fast_command_audit.log")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{ts} [{kind}] rc={rc} in={text!r} reply={reply!r}\n")
    except Exception as e:
        _log(f"审计日志写入失败: {e}")


def _clean_out(out):
    """去掉指令库诊断前缀行（UNKNOWN:/AMBIGUOUS:/ASK:/FAIL:），只留用户可见结果。"""
    lines = []
    for ln in (out or "").splitlines():
        if any(ln.startswith(p) for p in ("UNKNOWN:", "AMBIGUOUS:", "ASK:", "FAIL:")):
            continue
        lines.append(ln)
    return "\n".join(lines).strip()


def _extract_fail(out):
    """从 ha-ctl 输出提取失败详情：优先 FAIL: 前缀（支持 FAIL:原因 或 FAIL:名称|原因），其次 ❌ 行。"""
    for line in (out or "").splitlines():
        if line.startswith("FAIL:"):
            parts = line.split("|", 1)
            if len(parts) > 1 and parts[1].strip():
                return parts[1].strip()
            rest = line[5:].strip()  # 无竖线：剥掉 FAIL: 前缀（2026-10-08 修复：原先整行带回显「❌ FAIL:原因」）
            return rest or line
    for line in (out or "").splitlines():
        s = line.strip()
        if s.startswith("❌"):
            return s.lstrip("❌ ").strip()
    return ""


def _build_reply(out, rc, kind, args):
    # 面板型（kind=='panel'，指令库条目带 "panel": true）：脚本输出即完整面板，直接整份回显
    # （2026-09-05 配置驱动化：不再按关键词/名单匹配——新增面板只需在指令库加 panel:true 条目）
    if kind == "panel":
        if rc == 0 and out:
            return out
        detail = _extract_fail(out)
        return f"❌ {detail}" if detail else "❌ 面板获取失败"
    reply = _extract_reply(out)
    if reply:
        return ("✅ " + reply) if kind == "lib" else reply
    if rc == 0:
        cleaned = _clean_out(out)
        if cleaned:
            return cleaned
        return "✅ 已执行"
    detail = _extract_fail(out)
    if detail:
        return f"❌ {detail}"
    return f"❌ 快路径执行失败（rc={rc}）"


def _resolve_adapter(gateway, source):
    """解析发送用 adapter（Hermes 网关 API 兼容层）。

    ⚠️ 网关 0.21+ 把 `_adapter_for_source` 扫成 `_delivery_adapter_for`（多路复用 profile 归属
    改造），旧名直接 AttributeError ⇒ 快路径执行成功但回复**静默丢失**。此处按新→旧顺序探测，
    两边都缺则抛错走告警（不再无声吞掉）。
    """
    for name in ("_delivery_adapter_for", "_adapter_for_source"):
        fn = getattr(gateway, name, None)
        if callable(fn):
            return fn(source), name
    raise AttributeError(
        "gateway 未提供 _delivery_adapter_for/_adapter_for_source（API 变更，需同步插件）"
    )


_SEND_FAIL_FILE = "/opt/data/state/fast_command_send_fail.json"


def _record_send_failure(reason):
    """发送失败落盘 + 告警级日志（原实现只记 INFO，静默丢消息一整天无人知）。

    落盘供 cron/体检读取；连续失败会覆盖上一次，只留最新一条（防无限增长）。
    """
    _log(f"❌ 回复发送失败: {reason}")
    try:
        import logging as _logging

        _logger_obj = _logging.getLogger("fast-command")
        if _logger_obj.isEnabledFor(_logging.ERROR):
            _logger_obj.error("[fast-command] 回复发送失败: %s", reason)
        Path("/opt/data/state").mkdir(parents=True, exist_ok=True)
        with open(_SEND_FAIL_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "ts": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                    "reason": str(reason)[:300],
                },
                f,
                ensure_ascii=False,
            )
    except Exception as e:  # noqa: BLE001
        _log(f"发送失败落盘也失败: {e}")


def _clear_send_failure():
    """发送成功时清掉失败标记（下次体检不再误报）。"""
    try:
        if os.path.exists(_SEND_FAIL_FILE):
            os.remove(_SEND_FAIL_FILE)
    except Exception:
        pass


def check_gateway_compat(gateway):
    """启动自检：网关发送 API 是否可用。返回 (ok, detail)。

    ⚠️ 上游改私有名会让本插件静默失联（2026-10-14 实测断了一整天）：钩子仍命中、
    执行仍成功、只有回复发不出去。故注册时主动探一次，缺名即刻报错级日志。
    """
    names = [n for n in ("_delivery_adapter_for", "_adapter_for_source") if callable(getattr(gateway, n, None))]
    if names:
        return True, f"可用发送接口: {names[0]}"
    detail = "网关未提供 _delivery_adapter_for/_adapter_for_source —— 快路径回复将无法送达，需同步插件"
    try:
        import logging as _logging

        _logging.getLogger("fast-command").error("[fast-command] 启动自检失败: %s", detail)
    except Exception:
        pass
    return False, detail


async def _send_reply(gateway, source, content):
    try:
        adapter, via = _resolve_adapter(gateway, source)
    except Exception as e:
        _record_send_failure(f"取 adapter 失败: {e}")
        return
    if adapter is None:
        _record_send_failure(
            f"无可用 adapter（via={via} platform={getattr(source, 'platform', None)}）"
        )
        return
    try:
        result = await adapter.send(source.chat_id, content)
        if result is not None and not getattr(result, "success", True):
            _record_send_failure(f"适配器返回失败: {getattr(result, 'error', 'unknown')}")
            return
        _clear_send_failure()
    except Exception as e:
        _record_send_failure(str(e))


def _worker(text, kind, args, source, gateway, loop):
    reply = ""
    rc = -1
    out = ""

    # 待办序号查询（2026-09-05）：0-token 直读快照回摘要，不启动子进程、不唤醒 agent
    if kind == "todo-query":
        try:
            seq = int(args[0])
            scope = args[1] if len(args) > 1 else "active"
            snap_path = "/opt/data/待办/.last_panel_done.json" if scope == "done" else "/opt/data/待办/.last_panel.json"
            with open(snap_path, encoding="utf-8") as f:
                snap = json.load(f)
            hit = next((it for it in snap.get("items", []) if it.get("seq") == seq), None)
            if hit:
                icon = {"进行中": "🟢", "观察中": "👀", "待决策": "🤔", "已完结": "✅"}.get(hit.get("status"), "•")
                pri = {"高": "🔴", "中": "🟡", "低": "⚪"}.get(hit.get("priority"), "")
                created = (hit.get("created") or "")[5:] if len(hit.get("created") or "") >= 10 else (hit.get("created") or "—")
                lines = [f"第{seq}条：{icon} {hit.get('title','')} {pri}"]
                if hit.get("summary"):
                    lines.append(f"　  📅{created} · {hit['summary']}")
                lines.append(f"　  📄 {hit.get('file','')}")
                reply = "\n".join(lines)
                rc = 0
            else:
                reply = f"❌ 快照里没有第{seq}条（共{snap.get('count',0)}条），重新看待办刷新"
                rc = 2
        except FileNotFoundError:
            reply = "❌ 还没有面板快照，先「看待办」生成"
            rc = 2
        except Exception as e:
            reply = f"❌ 快照读取失败: {e}"
            rc = 1
        _audit(text, kind, rc, reply)
        try:
            if loop is not None and not loop.is_closed():
                asyncio.run_coroutine_threadsafe(_send_reply(gateway, source, reply), loop)
            else:
                _log("事件循环不可用，回复未发送")
        except Exception as e:
            _log(f"调度回复失败: {e}")
        return

    # 容错一次：仅网络/异常类重试；rc==2 是确定性语义失败（找不到/歧义/未命中），重试无意义（S 修复 2026-08-28）
    for attempt in (1, 2):
        try:
            r = subprocess.run(
                [sys.executable, HA_CTL] + args,
                capture_output=True, text=True, timeout=int(_load_config().get("fast_timeout", 30)),
            )
            rc = r.returncode
            out = (r.stdout or "").strip()
        except Exception as e:
            out = ""
            rc = -1
            if attempt == 2:
                _log(f"执行异常: {e}")
        else:
            if rc == 0 or rc == 2 or attempt == 2:
                break
    reply = _build_reply(out, rc, kind, args)

    # 面板上下文留痕（2026-09-05 主人定「追问定位」）：kind=='panel' 且成功时，
    #   把「最后发出的面板」全文写入通用快照 —— 主人面板后追问（如「第3行那个」「全屋状态里的X」），
    #   agent 先读此文件定位上下文，不凭空猜。待办面板另有 .last_panel*.json 精细快照（seq→文件映射）作补充。
    if kind == "panel" and rc == 0 and reply:
        try:
            import os as _os
            _os.makedirs("/opt/data/state", exist_ok=True)
            with open("/opt/data/state/last_panel.json", "w", encoding="utf-8") as _f:
                json.dump({
                    "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "trigger": text,
                    "reply": reply,
                }, _f, ensure_ascii=False, indent=2)
        except Exception as e:
            _log(f"面板快照写失败: {e}")

    _audit(text, kind, rc, reply)

    try:
        if loop is not None and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(_send_reply(gateway, source, reply), loop)
        else:
            _log("事件循环不可用，回复未发送")
    except Exception as e:
        _log(f"调度回复失败: {e}")


# ---------- 钩子 ----------

_compat_checked = False


def on_dispatch(event, gateway, session_store, **kwargs):
    """pre_gateway_dispatch：返回 {'action':'skip'} 吞掉确定性指令；否则 None。"""
    global _compat_checked
    try:
        # 首次命中顺便自检网关发送接口（进程内只跑一次，零额外开销）
        if not _compat_checked:
            _compat_checked = True
            ok, detail = check_gateway_compat(gateway)
            _log(("启动自检通过: " if ok else "🔴 启动自检失败: ") + detail)
        source = getattr(event, "source", None)
        if not _authorized(source):
            return None
        text = getattr(event, "text", "")
        hit = _classify(text)
        if hit is None:
            return None
        kind, args = hit
        # 捕获事件循环（回调在网关事件循环线程内同步调用）
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None
        threading.Thread(
            target=_worker, args=(text, kind, args, source, gateway, loop),
            daemon=True,
        ).start()
        _log(f"快路径命中 kind={kind} text={text!r}")
        return {"action": "skip", "reason": f"fast-command:{kind}"}
    except Exception as e:
        # 异常 -> 网关自动回退到正常派发（fail-open），agent 兜底
        _log(f"钩子异常（fail-open 回退 agent）: {e}")
        return None


def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", on_dispatch)