# 上游脚本接入执行器（骨架 + 注入矩阵实测样例）

适用：第三方开源脚本要跑在家里的定时链路上（cron + 加密库 + 飞书卡片）。规则见 SKILL.md「接入上游第三方脚本」节；选型/可行性评估见 env-dependency-audit 的 `references/oss-repo-triage.md`。

## 1. 目录布局（照抄）

```
/opt/data/<项目>/
├── upstream/          # git clone 的上游，pin commit 记在 ../upstream.commit，只读
├── venv/              # /usr/bin/python3.13 -m venv（容器默认 python3 可能是 3.14 且无 requests）
├── run.py             # 执行器：读凭据 → 预检 → 调上游 → 回读 → 推卡片
├── config.json        # armed / 凭据键名 / 策略参数 / chat_id / 通知开关
├── state.json         # 推送去重状态（last_result / last_notify_date / first_fail_date）
├── README.md          # 路径表 + 启用步骤 + 配置项 + 退出码 + 测试钩子
└── logs/
    ├── YYYY-MM-DD.log # 执行器明细（含上游输出，逐行打 `上游| …` 前缀）
    ├── cron.log       # cron 包装脚本重定向
    └── records.jsonl  # 每次真实运行的结构化记录（供日后周报/统计）
```

## 2. 执行器骨架要点

- **取凭据**：`sys.path.insert(0, "/opt/data")` → `from secret import cmd_get`，兜底再退 `bash /opt/data/get_secret.sh <KEY>`。
- **预检只读**：先打业务只读接口判定「凭据有效 / 未登录 / 任务已完成」再决定要不要拉起上游；**不要靠 HTTP 状态码判登录**，只认业务体里的身份字段。
- **调上游**：`subprocess.run([VENV_PY, "main.py"], cwd=UPSTREAM, env=env, capture_output=True, text=True, timeout=…)`；凭据/参数走 env，除非上游只认配置文件。
- **回读**：跑完再查一次进度接口，卡片里报「基础 x/y、加评 x/y」用的是回读值；回读失败时退回预检值，别让卡片显示「账号：未知」。
- **落记录**：`records.jsonl` 追加 `{ts, result, ok_count, score_dist, elapsed_sec, err}`。
- **CLI**：`--check`（只读自检、不推不写状态）、`--dry-run`（打印卡片 JSON）、`--selftest`（发一张自检卡验证推送通道）、`--force-notify`（忽略静默策略）。

## 3. 注入钩子（env 覆盖，正常跑不设）

| 钩子 | 实现位置 | 用途 |
|------|---------|------|
| `<PREFIX>_COOKIE_RAW` | 凭据读取处（`is None` 才回落加密库） | 空值 → 未配置；缺字段 → 凭据失效；真值 → 真调外部接口 |
| `<PREFIX>_PREFLIGHT_JSON` | 预检函数首行 | 离线伪造「已登录 / 今日已完成」 |
| `<PREFIX>_UPSTREAM_DIR`、`<PREFIX>_PYTHON` | 模块级路径常量 | 换成必然失败的假上游 |

模块级常量必须写成 `os.environ.get(...) or os.path.join(BASE, ...)`，否则测试改不动。

## 4. 六分支注入矩阵（实测样例）

harness 循环跑 `run.py --dry-run --force-notify`，改 env 注入各分支，再从 stdout 的 `---- DRY-RUN CARD ----` 里 `json.loads` 出卡片、抽 `elements[*].text.content` 核对：

```python
cases = [
    ("未配置凭据",   {"PREFIX_COOKIE_RAW": ""}),
    ("凭据缺字段",   {"PREFIX_COOKIE_RAW": "MUSIC_U=abc"}),          # 缺关键 cookie
    ("凭据失效",     {"PREFIX_COOKIE_RAW": "MUSIC_U=abc; __csrf=def"}),  # 真调接口
    ("成功路径",     {"PREFIX_COOKIE_RAW": "…", "PREFIX_PREFLIGHT_JSON": "pre_ok.json",
                     "PREFIX_UPSTREAM_DIR": "fake_ok", "PREFIX_PYTHON": "/usr/bin/python3.13"}),
    ("上游失败",     {…同上, "PREFIX_UPSTREAM_DIR": "fake_fail"}),
    ("今日已完成",   {"PREFIX_COOKIE_RAW": "…", "PREFIX_PREFLIGHT_JSON": "pre_done.json"}),
]
```

假上游只需 `print` 上游格式的日志行（`评分完成：3分` / `❌ 执行失败`），便于验证分数解析与致命判定。

## 5. cron 挂载

- 包装脚本 `~/.hermes/scripts/<项目>_daily.sh`：`exec <venv>/bin/python <项目>/run.py >> logs/cron.log 2>&1`，`exec` 保证退出码透传；`bash -n` 过一遍语法。
- 任务用 `no_agent=true`、`deliver="local"`（脚本自推卡片，避免 cron 再转投一份）、schedule 用 cron 表达式（如 `5 1 * * *`）。
- 建完用 `jobs.json` 回读确认 id / script / no_agent / deliver / enabled 落地。
- 手动复验三条：包装脚本裸跑看日志与退出码、`run.py --check` 真调外部接口、`run.py --selftest` 真发一张卡（`send_card` 返回 True 才算通）。

## 6. 实测样例（网易云「音乐合伙人」）

上游 `ACAne0320/ncmp`（纯 HTTP + 自实现 weapi 加密，无浏览器/无 GUI）；执行器 `/opt/data/music-partner/run.py`，凭据 `MUSIC_NETEASE_COOKIE`（含 `MUSIC_U` + `__csrf`），cron job `438d67108612` 每日 01:05。

- 依赖：`requests` + `pycryptodome` + `PyNaCl` + `pyncm`（后者不在 PyPI，需 `git+https://…` 安装，约 16 秒装完）。
- 上游日志解析：`评分完成：N分`（正则统计分值分布）、`❌ 执行失败`（致命）、`Traceback`（致命）、其余 ERROR 行仅作提示。
- 上游跑一轮约 4–6 分钟（每首间隔 15–20 秒防风控）——`run_timeout_sec` 给 900，别按秒级超时。
- `armed=false` 期间包装脚本裸跑应输出「未启用…静默跳过」且 exit 0。
