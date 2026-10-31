---
name: state-db-internals
description: "查 Hermes state.db 验会话/消息落库。落库守卫类验证、时间戳窗口查 session 时用。"
version: "1.23.2"
---

# State DB 查询与落库验证

## 适用场景
- **首要是触发匹配**：主人消息以「落库守卫 / 落库验证 / 首条消息落库 / 探针 round N」开头（常附毫秒时间戳整数），即属本技能——立刻加载并按下方流程查 state.db，**不要当成普通/含糊指令即兴改查别处**（如查加密库凭据）。家族前缀 + round 号是定位键，时间戳是交叉验证键，两者都来自该消息本身。
- **历史 round 复报先查重（高频省时点）**：同一探针家族反复多轮，主人可能隔数天后对早前的 round 重新发一遍消息。动手跑 SQL 前先查历史判定——`SELECT session_id FROM messages WHERE content LIKE '%round N %' AND role='assistant'` 找到上一轮判定所在 session，其尾行就是当时的结论（可配 session_search 全文检索佐证）。若结论与判据未变，直接引用历史结论复报，只跑最小核对 SQL（计数 user 行）；判定方法本身有更新时仍按完整流程重新执行，并对比新旧结论。**复报的判键是 ms 时间戳，不是 round 号**：先比对消息里的 ts_ms 与历史判定针对的 ts_ms——相同才是复报；**round 号相同但 ms 不同 = 新批次**（同家族同轮号隔天重发很常见），必须走完整三步流程，历史结论只作家系佐证，绝不可直接顶替本批判定。
- **⚠️ 探针消息家族隔离铁律（2026-09-21 实测）**：探针消息按家族独立查库，**绝不合并、绝不串查**——「落库守卫 round N」查 sessions/messages 表；「加密库 41 键」等**非消息类**家族探针与 state.db 无关，查加密库/SSH/hostname，不走本技能的三步落库流程；**「profile 归属验证」是归因类家族**——同样不跑三步落库流程，但**不等于与本库无关**：`sessions.profile_name` 与「会话行落在哪本库」是它最硬的一条证据，仍要查 state.db（四证法见 `hermes-troubleshooting` 第 7 节），只是结论按自己家族独立出、不套落库守卫的判定式。**但消息/房间类家族（Discussion 正文形如「<家族名> <ms>」的接线验证、注入逐字核对等）仍要查 state.db 的 user 行**，房间机制问题再转 `references/hosted-room-diagnostics.md` 查 shared-state.db——只是结论按自己家族独立出，不是不查本技能的库。**家族名一律取自消息正文，不取房间标题**：信封 `[Discussion: "…"]` 里是房间名，可与探针家族完全不同（实测标题是落库守卫某 round、正文是另一家族）。主人连续发多条不同探针时，逐条独立执行并分别上报，不得把多条合并成一次批量处理、不得用一条的结论覆盖另一条。
- 验证某会话/探针轮次的首条 user 消息是否已写入 messages 表（「落库守卫」类检查）
- 按时间戳窗口或标题片段定位 session
- 区分「请求未送达（session 未创建）」与「session 已建但 user 消息未写入」两类失败
- E2E 接力测试的 B 会话：入站为「【交接摘要】+ UI-E2E-原话-NNNNNN」、标题「接力UI源-*-b」，验「灰条与 A 历史拼接（UI-E2E-SUMMARY-MARK）」——按 `references/relay-handoff-e2e.md` 取证与收口，**不要全盘翻 /opt/data 找标记**
- 多参与方讨论（Discussion / bot_room）判「谁收到、谁没收到、为什么」：按 `references/cross-profile-delivery.md` 逐 profile 库取证。**首要是「信封参与方名单 ≠ 投递名单」**——信封声明了 X 不代表 X 收到，必须查 X 自己的库；首条按 @ 触发、入场后续投两段分开验
- 官方房间（hosted room / bot_room Discussion）内部机制诊断——seq 编号稳定性、派发链断裂（水印滞后/supersede 无后继/取消未生效/**回合超时后迟到回复只落库不进房**）：按 `references/hosted-room-diagnostics.md` 查 **`shared-state.db`**（⚠️ state.db 的 hosted_room_* 系表恒为空，查错库会得「无数据」假结论）

## Discussion 接线/折叠组 rounds 的定位与取证（消息类家族并发多轮）
- 主人以 Discussion 房间连发多轮验证（正文形如「<家族名> <ms>」：折叠组接线验证 / 折叠组验收 / 首条消息落库守卫 round N）时，**每轮各开独立 session 并行跑**——同刻库内存在兄弟轮 session，窗口内「最新 user 行」往往属于兄弟轮而非本轮。**不要按最新 user 行定位本轮**，用两条硬锚：①`$HERMES_HOME/desktop/interrupted_turns.json`——每个 round 的 session id 与 prompt 信封全文一一对应，按 ms/家族名匹配即得本 session；②全库 `WHERE content LIKE '%<ms>%'` 扫，`session_id` 与①交叉核对。家族名一律取消息正文，不取信封房间标题（房间标题常是另一轮家族的）。
- **`messages` 表同样没有 `created_at` 列**——时间列只有 float 秒 `timestamp`，`id` 是整数；写 `created_at` 直接报 `no such column`。排序/窗口一律走 `timestamp`，会话创建时刻看 `sessions.started_at`。
- 接线轮回复口径：报「家族名 + ms + 落点 session id」确认收到，再 @ 队友核另一端落库；引用历史同家族轮作家系佐证时判键仍是 ms 相同（不同 ms 只作家族先例，见「历史 round 复报先查重」）。想找早前同家族轮上下文时，`/opt/data/memory/sessions/` 下按家族名搜会话快照可一步定位。

## 一键取证脚本（优先跑它，别手敲 8 段 SQL）
`scripts/verify_probe_landing.py <ts_ms> <标题片段> [--round N] [--db PATH]`
——只读打开 state.db，一次跑完 Step 0/1/2 三档窗口、锚点选取（三级匹配：标题命中本 round → 同家族 → 邻窗，同级内取时距最近者，**不按 message_count 排序**）、锚点首查取证、整批复核、跨 session 逐列扫 ms（自动剔除本轮自产行）、对照实验、id 空洞 frontier 法、锚点漂移复核，并按固定优先级打印一行 VERDICT。
⚠️ **输出必须整段核对，不能只看 VERDICT 那一行**：锚点若被同窗其它会话抢走，VERDICT 仍可能因「跨 session 同家族 user 行≥1」而判「已落库」，**结论对而取证段整段是另一会话的无关数据**（见「首条 user 行定位陷阱」相应条目）。脚本在锚点标题未命中本 round 时会打 ⚠️，见到就按三级匹配手动重选。
用法（脚本在 `scripts/` 子目录，**给到文件本身**——别 cd 到技能目录后用相对文件名跑）：
`python3 "$HERMES_HOME/skills/ops/state-db-internals/scripts/verify_probe_landing.py" 1791123677089 首条消息落库守卫 --round 3`
⚠️ **报 `can't open file ... No such file or directory` 而 `search_files`/`stat` 又显示文件实在**：后台刚写入的技能文件偶发短暂不可见，核实存在后**直接重跑一次**即可（内容可读、`ast.parse`/`compile` 通过即非损坏）；别据此断定脚本缺失，也别为一个不存在的错误去改写脚本内容。
**脚本是手写 SQL 的固化，不替代判读**：VERDICT 前的判定优先级清单要人读一遍；输出与手查不一致时以手查为准并回来修脚本。新库/新列结构首次使用先核对输出。
脚本自身的只读性与正确性验证见 `references/dispatch-path-verification.md`（与派发链路相关的通用取证规则）。

**接力链核对（UI-E2E-SUMMARY-MARK）另有专用一键脚本**：
`scripts/verify_relay_chain.py <B1> [B2 ... Bn] [--a <A id>] [--db PATH]`
——各 B 首条 active user 行三要素 + `## 目标` sha256 全链比对 + 原话标记互异 + display_kind=None 检查 + A 注销两阶段状态 + 灰条样本判定（hidden ∧ `_compressed_summary=1`），逐行打印可直接抄进收口报告。⚠️ **灰条一行不能只看计数**：脚本先打存量统计、再打 hidden 构成与 standalone 样本数——standalone 样本为 0 时，UI 项只能写「未验证/能力边界」，**禁止拿 hidden 行总数当「灰条生产实证」**（hidden 主体是打断占位）。B id 链按 `started_at` 升序给全（**含当前会话自己，别剔除**）；首条 user 行缺 `## 目标` 或无原话标记会即时退出并提示该 id 混入了 A 会话。接力任务不要再每轮手拼 SQL——照抄上一轮的 verify 脚本还会踩列名坑（见 SQL 陷阱）。

## ⚠️ TUI 通道 user 行已知盲区（2026-09-20 实测）

**TUI 通道的 user 首条消息常以 `role='assistant'` 或嵌入 tool_calls 列落库，而非独立的 `role='user'` 行。**

**⚠️ TUI 会话 title 不恒 NULL、user 行也可独立落库（2026-10-21 落库守卫 round 2 实测）**：Discussion/折叠组多轮探针现在每轮各开 `source='tui'` 的独立 session，且 **title 带家族名+round+ms 精确命中**（本轮 `20261021_052552_13d21e`，`title='首条消息落库守卫 round 2 1792531550186'`，Step 0 `WHERE title = '<前缀> round N <ms>'` 直接命中），user 行亦**独立落库**（id=91534、`role='user'`、content 精确等于探针原文、滞后探针 +18.5s）。判读三则：①TUI 探针先跑 Step 0/1 标题匹配，命中即按 API 通道正常流程走，三级匹配锚点照常（本 round title 已含 ms，不存在按 mc 降序抢锚的空间）；②「title=NULL」「user 行=0」只对更早批次／确无标题的纯 TUI 会话成立，**不得因 `source='tui'` 就预设盲区**；③同窗兄弟轮（本轮 round 1/3/4 与「文档预览验收」家族）各自独立 session 各恰好 1 条 user 行，齐备即「落库通道整体健康」的正向对照。 实测 round 9 验证 session（`api_1789851935_5b6fc4d0`）：23 条消息 = 13 assistant + 12 tool，**0 条 user 行**，且 ±60s 全表 `role='user' AND content LIKE '%1789851932327%'` 亦为 0。session `title` 为 NULL（TUI 通道不自动设 title），`source` 亦为 NULL。

**判读规则**：
- 若探针通道是 **TUI**（`source='tui'` 或 `source IS NULL`，`title IS NULL`），Step 3 user 行 = 0 是**已知模式**，不代表写入失败——user 消息被并入 assistant 首轮或 tool_calls 列。TUI 会话的 source 实为字面字符串 `'tui'`（session id 前缀 `api_<ts>_<hex>` 不等于 source 为 null；仅早期实测记录到 source=NULL 的 TUI 会话）。盲区判据只看 `title IS NULL` + source 属 tui/NULL 两类，勿硬套 `source IS NULL` 漏判。
- 若探针通道是 **API**（`source='api_server'`），user 行 = 0 才按原逻辑判归属错位或未送达。
- 区分方法：`SELECT source FROM sessions WHERE id='<id>'`；值为 `'tui'` 或 NULL → TUI 通道，按盲区处理。
- **TUI 同批多 session 现象（2026-09-22 round 1 实测）**：TUI 探针常在 ±5s 内同时创建**两条** source='tui'、title=NULL、message_count=1 的 session（user 首条并入其中一条的 assistant 首轮，另一条为后续轮次）。窗口内看到多条均符此特征时，**全部按盲区处理**，不得判「归属错位」或「请求未送达」；逐条查 `source` 确认，再跨 session 全文检索探针 ms 确认无独立 user 行即可定案。
- **TUI 持续会话探针**：TUI 探针对**活动会话**发起时，窗口（±5s/±60s）内往往**查不到任何** source='tui'、title=NULL 的独立探针 session（探针消息直接并入既有活跃 TUI 会话，不新建 session）。此时 Step 2 窗口空 ≠ 请求未送达——改锚定**正在累积**的 TUI session：取窗口附近 `source='tui' AND title IS NULL` 中 **`message_count` 最高且持续增长**的一条（窗内若全是 `message_count=1` 残桩，按上节放宽到 ±300s 再找），逐角色统计（`SELECT role,count(*) ... GROUP BY role`），user 行=0 即按 TUI 盲区定案，**勿判「请求未送达」**。
  - ⚠️ **此处「mc 最高优先」不与 §Step 2 的「不按 mc 排序」矛盾**：边界就是**能否用标题区分本批**——TUI 通道 `title IS NULL`，没有标题可做 round 匹配，mc 最高是仅存区分信号；三级匹配口径只在**标题可辨**（API 通道 / 有 title）时适用。

## 数据库位置
`$HERMES_HOME/state.db`。**切勿硬编码 `~/.hermes` 或 `$HOME/.hermes`**——不同部署 `HERMES_HOME` 落在不同卷。动手前先定位真实 DB：
```bash
echo "$HERMES_HOME"; ls -la "$HERMES_HOME/state.db" 2>/dev/null || find / -maxdepth 5 -name state.db 2>/dev/null | head
```
- 拿到的绝对路径传给 `sqlite3.connect(path)`（WAL 模式，读方不持排他锁）。**default profile 的 state.db 实际在 `/home/agent/.hermes/state.db`（`HERMES_HOME=/home/agent`，2026-09-20 落库守卫 round 4 实测，DB 约 248MB、内容活跃）；⚠️ `/opt/data/.hermes/` 下有**诱饵文件**——同名 state.db 是 0 表空库、`logs/agent.log` 是陈旧旧档（572889 轮实测：grep 诱饵 agent.log 对当轮接力标记零命中，差点误判「A 会话无记录」）。取证 agent.log 同样取 `$HERMES_HOME/logs/agent.log`（活性件，文件尾即本轮）。查库/查日志前先 `find` + 看 mtime/表清单确认真实文件所在，防止查错目录得出「user 行=0」或「无记录」假结论。**

## 两张关键表

### sessions
- `id`（如 `api_1789847059_c7eb6f87` 或 `YYYYMMDD_HHMMSS_<hex>`）
- `source`（`tui` | `api_server` | ...）
- `started_at`（float epoch **秒**，如 `1789847066.0069`）
- `message_count`、`title`（可 NULL；自动标题或探针标记字符串）
- `end_reason`、`archived`、`hidden`

### messages
- `session_id`、`role` ∈ {`user`, `assistant`, `tool`, `session_meta`}
- `content`、`timestamp`（float epoch **秒**）
- `observed`/`active`/`compacted` 标志、`token_count`、`finish_reason`
- **user 角色行 = 会话中实际落库的 user 消息**；`session_meta` 角色行 content 常为 NULL/空，忽略

## 单位陷阱（高频）
- `started_at` / `timestamp` 是**秒**（float）
- 探针/测试方通常给**毫秒**整数（如 `1789847091779`）
- 转换：`ts_sec = ts_ms // 1000`（或 `ts_ms / 1000` 得 float）
- 直接用 ms 整数对 seconds 列开窗，窗口偏 ~1789 倍，返回 0 行但看似正常——先转再查
- **B 摘要长度有两个口径**（接力类任务高频）：交接摘要 Python `len()` 数得字符数（三要素齐全时约 77 字符），UTF-8 字节数约 133（中文每字 3 字节）——跨轮/跨报告比摘要长度前先统一口径，「77 ≠ 133」不是截断证据；「零丢字」判定一律用 `## 目标` 字段 sha256，长度只作辅助

## 验证流程（三步）

- 给定探针标记（标题片段）+ 目标 ms 时间戳：

**Step 0 — 先做精确标题匹配**：同家族探针标题常**不带** ms 时间戳（如 `首条消息落库守卫 round N`），而探针消息标记带 ms（`round N <ms>`）。因此 Step 1 的 `LIKE '%round N%'` 会混入**带 ms 后缀的旧批次**——它们指向更早的历史探针，不是本批。判读前先跑一次精确匹配排除：
```sql
SELECT id, source, started_at, message_count, title FROM sessions
WHERE title = '<家族前缀> round N <ts_ms>';
```
命中的行才是本批；Step 1 里混入的带 ms 后缀旧批次仅作家族前缀确认用，其 started_at 若落在目标窗口外（如前一天）一律不引用。

**Step 1 — 按标题定位 series，再窄到 round**
```sql
SELECT id, source, started_at, message_count, title, end_reason
FROM sessions
WHERE title LIKE '%首条消息落库守卫%' AND title LIKE '%round 3%'
ORDER BY started_at DESC LIMIT 10;
```
⚠️ `LIKE '%round 3%'` 会命中其他探针家族（如「路由竞态探针 round 3」）——必须确认 title 的**家族前缀**，不能只看 round 编号。
⚠️ **TUI 探针 Step 1 常落空**：TUI 通道 session `title=NULL`（不自动设标题），Step 1 的 `LIKE '%家族前缀%'` 命中的往往全是历史批次（更早的 TUI/API 轮次），本批 round 无 title 命中。这是正常模式，**不得据此判「请求未送达」**——判定主力落到 Step 2 时间窗口 + Step 3 `SELECT source FROM sessions`；Step 1 无命中时直接跳 Step 2 继续，不要停在 Step 1。

**Step 2 — 时间戳窗口交叉验证**（±5s → ±60s → ±300s 逐级放宽，**终判以累积型 session 为准**）
```sql
-- 先 ±5s；空则放宽到 ±60s 查一次；**仍空（或只命中残桩）继续放宽到 ±300s**，终判以「有无累积型 session」为准
SELECT id, source, started_at, message_count, title
FROM sessions
WHERE started_at >= <ts_sec-5> AND started_at <= <ts_sec+5>
ORDER BY started_at;
```
⚠️ TUI/API 探针通道在目标时刻后 **0～数秒**内才创建 session（agent 首条响应写库后才见 session 行，TUI 实测延迟约 0.2～1.2s）。api_server 批量探针实测 `sessions.started_at` 可晚于探针 ts 约 +3s，而 `messages` 里 user 行的 `timestamp` 再晚十余秒（实测 +12.4s）——**查 user 行必须开 ±60s 窗口，不能拿 ±5s 内没查到 user 行就判「未落库」**。±5s 内 0 条 ≠ 请求未送达——**必须放宽到 ±60s 再查一次**。宽窗查到 session 后，对照 started_at 与 ts_sec 的偏移，注明 session 创建相对目标时刻晚了多少秒。

**session id 内嵌 epoch 是比 started_at 更强的送达/创建证据**：`api_<epoch>_hex` 的 epoch 是 id 生成（会话创建）时刻，恒不晚于 `started_at` 且常早数十秒（实测 id 时刻=探针 +1.2s 而 `started_at`=+56.3s；旧批 +8s vs +79s）——判「请求已送达」优先比对 id 内嵌 epoch 与 ts_sec，**`started_at` 落在窗外不构成未送达**。

**⚠️ ±60s 不是判定边界，「窗口里有没有行」也不是判据——判据是「有没有一条 message_count 与本轮运行时长相称的累积型 session」**（实测踩中：真正的活跃 TUI 会话在目标 +62.6s 处，完全落在 ±60s 之外，而窗内 6 条命中**全是残桩**）：
- 窗内命中若**全是残桩**（`message_count=1` + title NULL + roles 只有 assistant），等于**没命中**——继续放宽到 **±300s**，`ORDER BY started_at DESC` 逐级外扩，直到找到 `message_count>1` 的候选或本轮活动窗口确已结束。只有「±300s 内不存在任何累积型 session」才允许判「请求未送达 / 网关丢弃」。
- **残桩既不能当证据也不能当锚点**：一次探针窗内可冒出 5～6 条，`started_at` 密集、彼此相差 1～5s，极易被「取 started_at 第一条」误锚成探针 session；而残桩的 role 分布必然 user=0，锚上去会**稳定得出错误的「TUI 盲区」结论**——把「找错了锚点」伪装成「已知正常模式」，比直接报错更危险。**先按「标题精确命中本 round → 同家族 → 邻窗」三级匹配选锚点，同级内取 `started_at` 最靠近目标时刻者**，绝不按 `started_at` 取首条、**也绝不按 `message_count` 降序取最大**——同批多个 round 常在 ±300s 内一起落库（实测 round 1~11 同时在跑），越靠后的 round 累积轮次越多、mc 越大，按 mc 降序会把邻轮抢成锚点，导致「锚点取证/漂移复核」查的不是本批、而「跨 session 同家族 user 行」查的却是本批，三段口径打架给出错误结论（2026-10-05 实测：round 1 mc=37 被 round 2 mc=22 抢锚；同 round 但 ms 不同的旧批次 user 行也会被误算成本批证据）。

⚠️ **命中即取证（首查一次取全）**：锚定候选 session 的**第一次**查询必须把 `source/title/started_at/message_count` 与 `SELECT role,count(*) FROM messages WHERE session_id='<id>' GROUP BY role`、首条 user 行预览一次性取全。覆盖复用可能发生在两轮查询之间（实测间隔仅半分钟）——先只取元数据、回头再取 role 分布，拿到的已是被覆盖后的值，「首条 user 行是否曾落库」就永远失去直接证据，只剩间接推断。

**Step 3 — 首条 user 消息落库判定**（一条 SQL 定案）
```sql
SELECT count(*) FROM messages WHERE session_id='<id>' AND role='user';
```
- `0` → 首条 user 消息未落库（session 行仅从 assistant/tool 侧写入）
- `>=1` → 已落库；可用 `SELECT substr(content,1,120) FROM messages WHERE session_id='<id>' AND role='user' ORDER BY id LIMIT 1` 确认内容

## 首条 user 行定位陷阱（高频）
- **锚定 session 元数据漂移 = 记录被覆盖复用（round 5 2026-09-21 实测）**：Step 1/2 命中一条 session（如 `title='某家族 round N'`、`source='api_server'`、特定 `started_at`、`message_count`），但**复核同一 id 时这些字段全部变了**（source、title、started_at、message_count、消息 id 区间），且新值指向另一通道/另一时刻的会话——这是**探针 session id 被后续会话复用覆盖**，原始 api_server 记录连同其 user 首条消息整体被抹掉，跨 session 全文检索探针 ms 亦仅命中 assistant 回显。判读：先 `SELECT id,source,started_at,message_count,title FROM sessions WHERE id='<id>'` 与初查值逐字段比对；任何字段漂移就要重新按 ts 窗口+title 精确匹配确认原始记录是否仍在，勿用「user 行=0」直接落到盲区/归属错位（那是 source/title 未漂移时的判据）。
- **覆盖复用是整批现象，不是单 id 偶发**：同批其它探针 session 常在分钟级内一并消失（title 精确匹配查无）或被 `source='tui'` 的新会话复用同一 id（source/title/started_at 全换）。锚定 id 漂移后必须回查 ±60s 窗口内**同家族全部 round** 的现状——整批消失/整批复用互为佐证，也是「原始记录不存」的定案依据；只盯一个 id 会漏判覆盖面。
- **messages.id 空洞 = 删除铁证**：`SELECT id FROM messages WHERE session_id='<id>' ORDER BY id` 若 id 序列出现大段缺口，说明原始行已被 DELETE；配合「全库该 ms 无命中」即可定案原始记录连同首条 user 消息不再留存，无需再找归属错位解释。
- **锚点被同窗其它会话抢走时，结论可能「侥幸正确」而取证段全是无关数据**：实测探针批与别的家族会话（`MD-LINK-PROBE`、tui、mc=14）同窗共存时，锚点会被抢走，真探针 session 反被排到第 4——此时 VERDICT 仍可能因「跨 session 同家族 user 行≥1」而判「已落库」，**结论对、取证段却整段是另一会话的无关数据**，照抄进报告会污染证据。判读时必须**独立核对脚本报的锚点 title 与目标批次是否一致**（不一致就按 §Step 2 的三级匹配口径手动重选），不能只看 VERDICT 就信整段输出。
- **锚点选取唯一口径（三级匹配，不按 message_count 排序）**：见 §Step 2「残桩既不能当证据也不能当锚点」条——`本 round 标题命中 → 同家族 → 邻窗`，同级内取 `started_at` 最靠近目标时刻者。**任何「先锁同家族、再按 message_count 降序」的变体都是错的**：同家族内部同样会被 mc 更大的邻轮抢锚（见该条实测），单纯解决不了跨轮抢锚。
- **三级匹配仍会系统性选错的一种形态（⚠️ 触发后不可重跑同一套匹配）**：同家族**带 title 的邻轮**（如 round 12，title 齐全）对阵本批 **title=NULL 的真承载会话**——同家族级别高于邻窗，邻轮必然抢过本批，重跑三级匹配得到的还是同一个错锚。见脚本 ⚠️ 后改用两条定位法：①**认领跑本次验证的当前会话**——TUI 探针常直接落在正在执行验证的活跃会话上，判据=当前会话 id 内嵌时刻紧贴探针 ms、首行 reasoning 含探针原文、mc 本轮持续增长；②以探针 ms 逐列扫取**最早的非本轮自产行**所在 session 为承载会话。定锚后照常「命中即取证」。
- **id 空洞用 frontier 法量化（区分「从没写过」与「写了被删」）**：单看某 session 内的 id 缺口会与「该 session 本就只写了几条」混淆。取 `b = max(id) WHERE timestamp < ts_sec`（探针时刻之前的最后一个 id）与 `a = min(id) WHERE timestamp >= ts_sec`（之后第一个 id），再数 `range(b+1, a)` 里缺哪些 id——缺的是**探针本该占的位置**，跨 session 的空洞才是删除证据。实测一轮探针批可留下数十个连续空洞。
- **对照实验：同窗口全库 user 行=0 → 不是归属错位**：判「user 行没落库」前先数窗口内**任何内容**的 `role='user'` 行（±60s~+300s）。若连一条都没有，说明这一整段根本没写过 user 行——「归属错位」的前提（同窗口别处有 user 行）不成立，**只排除归属错位、不排除 TUI 盲区**：TUI 承载会话本就不写独立 user 行，窗口 user=0 恰是同期全 TUI 会话的常态；锚点 source∈(tui,NULL)+title NULL 时仍按盲区定案，**勿据此推「原始行被删除」**（窗口无 user 行 ≠ 有过 user 行又被删）。再看全库最后一条 user 行距目标时刻多久（>0 即探针时刻之后全库再无 user 行），这条时间差本身就是结论的一部分，写进上报。
- **同批兄弟 round 是天然对照组**：一批守卫探针常在两三分钟内**全部**落库，各自独立 `api_server` session、每 session 恰好 1 条 user 行。本批 user 行落地时同批兄弟行一并出现，即「落库通道整体健康」的正向证据（比单批孤证更硬）；反之兄弟行缺失而仅本批缺，才值得往写入失败方向查。
- **user 行归属 ≠ 你锚定的 session**：探针的 user 行常落在同批另一 session（TUI/API 通道对探针消息与后续 agent 轮次分 session 记账）。Step 3 在「你以为的那个 session」里 `role='user'` 得 0 行，**不等于**首条 user 未落库——先跨 session 查：`SELECT session_id, substr(content,1,80) FROM messages WHERE content LIKE '%<家族前缀>%<round N>%' AND role='user'`，再看它挂在哪个 session 上。
- **探针全文 ≠ user 行 content**：agent 复述的探针文本常落在 `assistant`/`tool` 行（被回显在输出里），而真正的 user 首条是**精确匹配原文**的那一行。看到探针文本就判「已落库」是误判——文本在库 ≠ 以 user 角色落库，两条要分开证。全文扫也不要只扫 `content`——`tool_calls`/`api_content`/`reasoning*` 列同样存文本，逐列扫才不漏；但**逐列扫会自命中你自己本轮的查询代码与思考回显**（探针 ms 字面量出现在你的 SQL 参数/分析里），计数前必须剔除本 session 自产行，否则自证当成了落库证据。
- **探针 title 常带 `[probe:xxx]` 前缀**：`[probe:newsession_intro] 首条消息落库守卫 round 3` 这类 title 使 Step 0 的 `title = '<前缀> round N <ms>'` 必然 0 行、Step 1 的家族前缀也不在字符串开头。**Step 0 精确匹配落空是常态、不阻断流程**，Step 1 一律用 `LIKE '%<家族片段>%'`（前导 %）匹配；反过来命中带 `[probe:` 的行说明就是本家族。
- **逐列扫的自命中剔除用 timestamp 阈值而非列举 session**：探针 ms 字面量必然出现在你自己本轮的 SQL 参数/思考回显里。除按 session_id 剔除外，更稳的做法是加 `AND timestamp < <本轮首次查询时刻>`——本轮之前落库的行都不含该字面量，一次条件就把自产行全排掉，不用逐条列举。
- 判定式（一条 SQL）：`SELECT count(*) FROM messages WHERE content LIKE '<家族前缀> round N %' AND role='user'`；`=0` 且按累积型 session 口径（**±300s 逐级外扩，见 Step 2**）确无候选才判「请求未送达」；`=0` 但 session 已建、同窗口其他 session 有同家族 user 行，则是归属错位，勿报「写入失败」；同窗口**连无关 user 行都没有**时按上条对照实验口径判（原始行未留存/被删），不是归属错位。
- **同批 session 锚定规则（round ≥ 4 起出现）**：`title` 带 `#N` 序号（如 `首条消息落库守卫 round 12 #4`）表示同 round 内多次复发的 session。跨 session 查 user 行时用 `substr(content,1,80)` 看实际 ms 时间戳段，再与目标 ts_ms 比对——同家族同 round 的早批/中批 user 行不能替本批作数。**⚠️ 不带 `#N` 时同样要用 ms 收敛**（2026-10-05 实测）：round 号会跨批次复用（换天重发、批次内多 round 并行），`LIKE '%round N%'` 会命中历史批次的同号 user 行（实测一次命中 3 条不同 ms 的旧行）。判据是 **ms 相同才是本批**，故跨 session 检索须加 `content LIKE '%<ts_ms>%'`，旧批次单列参考、不计入本批证据。
- **TUI 通道盲区（2026-09-20 实测）**：TUI 通道（`source='tui'` 或 `source IS NULL`，且 `title IS NULL`）的 user 首条消息常以 `role='assistant'` 落库或并入 `tool_calls`/`api_content` 列，**独立 `role='user'` 行 = 0 是已知正常模式**，不能据此判「写入失败」或「归属错位」。判读前必须先查 `SELECT source, title FROM sessions WHERE id='<id>'`：source=NULL → TUI 通道，user 行=0 为已知盲区；source='api_server' → 按原逻辑判归属错位或未送达。

## 从会话库重建被删文件（tool_calls 回放）

目录/文件被误删、又没有快照或备份时，state.db 里存着当时的 `write_file` / `patch` / `read_file` 参数，可以把文件内容重放出来。

```python
import sqlite3, json, re
c = sqlite3.connect("$HERMES_HOME/state.db"); c.row_factory = sqlite3.Row
# ⚠️ tool_calls 里的中文以 \uXXXX 转义存储：LIKE '%待办%' 恒为 0 命中
rows = c.execute("select id,timestamp,tool_calls from messages "
                 "where tool_calls like '%u5f85%u529e%' order by timestamp").fetchall()
```

- **中文路径必须按转义串检索**：`tool_calls` 列是 JSON 文本，非 ASCII 字符存成 `\u5f85\u529e`，直接 `LIKE '%待办%'` 一条都查不到（会误判「库里没有」）。先用目标路径里的 ASCII 片段或单个 `%u5f85%` 粗筛，再在 Python 里 `json.loads` 解出 `arguments` 精确比对路径。
- **三类记录按时间序合并**：`write_file.content`（最完整的快照，优先）→ `read_file` 结果（形如 `N|line`，逐行剥掉 `^\d+\|`）→ `patch.old_string/new_string`（顺序 `replace(old,new,1)`，统计 applied/miss；miss 说明快照晚于该 patch，可接受但要在汇报里注明）。
- **`read_file` 结果不在同一行**：工具调用在 `messages.id=X`，结果通常是其后 1~3 条 `role='tool' AND tool_name='read_file'` 的消息，取第一条非空 content。
- **只被 terminal（heredoc/cat）或 `mv` 创建的文件没有 write_file 快照**：能从 terminal 命令的 heredoc 里抠正文就抠，抠不到就如实报「该文件只剩标题引用、无法重建」，**不要凭标题编内容**。
- **产物先落暂存目录**（如 `/opt/data/tmp/<task>_recovery/`），核对字节数与首行后再写回目标；写回前先确认目标路径是活的（挂载可能已悬空，见 `fnos-app-deploy` 的悬空 bind mount 条），否则写进去会随容器重启蒸发。

## 统计真实工具调用次数（技能使用度 / 工具用量分析）

主人问「某个技能/工具到底用过几次」「哪些技能是零使用」时，**只能从 state.db 的 `tool_calls` 列解 JSON 计数**——这是「真实调用」的唯一来源。

```python
import sqlite3, json
c = sqlite3.connect("/home/agent/.hermes/state.db"); c.row_factory = sqlite3.Row
cur = c.cursor()
cur.execute("SELECT tool_calls FROM messages WHERE tool_calls IS NOT NULL AND tool_calls != ''")
real = {}
for r in cur:
    try: tc = json.loads(r["tool_calls"])
    except Exception: continue                      # 非法 JSON 行直接跳过
    for t in (tc if isinstance(tc, list) else [tc]):  # 可能单对象也可能数组
        fn = t.get("function", {}) if isinstance(t, dict) else {}
        if fn.get("name") == "skill_view":
            try: args = json.loads(fn.get("arguments", "{}"))
            except Exception: args = {}
            n = (args.get("name", "?") or "?").split(":")[-1]   # 去掉 plugin:/命名空间前缀
            real[n] = real.get(n, 0) + 1
```

换 `fn.get("name")` 的比对值即可统计任意工具（如 `delegate_task`、`browser_exec`）。

### 🔴 三个必踩的坑

1. **别用 `content LIKE '%名字%'` 数 SQL**：系统提示每轮把**全部技能的 description 注入 messages**，`LIKE` 会把「索引里出现过」误算成「调用过」（实测某技能 content 命中 158 次，真实调用只有 1 次）。**必须解 `tool_calls` 列，不能扫 `content`**。
2. **别用 `~/.hermes/skills/.usage.json` 的 `use_count` 判使用度**：该字段对**早期安装的技能根本没有记录**（这些条目 `created_at=None`、`use_count=0`），据此判「零使用」会误判——实测某技能被误判为 0 次，实际 state.db 里有 2 次真实调用。`use_count` 只在近期由本机创建的技能上可信；要全量准确只能查 state.db。
3. **`tool_calls` 列格式不稳**：可能是单对象、可能是数组，且部分行为空字符串——解 JSON 前先过滤空值 + `try/except`，再统一变列表遍历；`arguments` 是**字符串**需二次 `json.loads`。

### 交叉验证（把统计结果落地成线索）

统计出的调用次数配合时间戳能定位「何时用的」——`messages.timestamp` 是 float 秒，`datetime.fromtimestamp()` 转北京时间即可列出每次调用的时刻，用于判断某技能是「早期试过就不再碰」还是「近期高频」。

## 两类失败的区分（诊断要点）
| 现象 | 失败类 | 排查方向 |
|---|---|---|
| Step 2 窗口内 0 条 session | 请求未送达 / 网关路由丢弃 | gateway.log / 探针调度方（**判据须走累积型口径**：±300s 逐级外扩仍无 `message_count>1` 的候选才成立；仅 ±60s 空不作数，见 Step 2） |
| session 存在（source='api_server'），`message_count>0`，user 角色行数 = 0 | session 行先建，首条 user 消息未写入 | 查看同窗口 messages.timestamp 各角色实际落库情况；可能是压缩/rewind 替换 |
| session 存在（source='tui' 或 source=NULL 且 title=NULL，TUI 通道），`message_count>0`，user 角色行数 = 0 | TUI 通道已知盲区，非写入失败 | user 消息被并入 assistant 首轮或 tool_calls 列；无需告警，标记「已知 TUI 模式」即可 |
| Step 2 窗口内**无** TUI 探针 session（0 条 tui/NULL），但窗口附近有一条 message_count 持续增长的活跃 TUI 会话（title=NULL，user 行=0） | TUI 持续会话探针：探针并入既有活跃会话而非新建 session，非写入失败 | 改锚定活跃 TUI 会话逐角色统计，user=0 按盲区定案，勿判「请求未送达」（「窗口 0 条」仅对**无活跃会话**场景才成立） |
| 同一 session id 复核时 source/title/started_at/message_count 与初查漂移，原始 api_server 记录查无 | 探针 session id 被后续会话复用覆盖，原记录连 user 首条被抹掉 | 重新按 ts 窗口+title 精确匹配确认原始记录是否仍在；原始 user 行已无独立留存 |

## SQL 陷阱（此库实测）
- 无 `left()` 函数——预览用 `substr(content,1,N)`
- **`sessions` 表没有 `created_at` 列**，时间列是 float 秒 `started_at`——写 `created_at` 直接报 `no such column`，窗口查询一律 `started_at BETWEEN <epoch秒> AND <epoch秒>`
- session `id` 字符串序 ≠ 时间序——**永远按 float `started_at` 列排序**，不按 id
- `title` 有 UNIQUE 索引但允许 NULL，自动标题常留空——查找必须带系列标记片段，不能只靠 title
- `assistant` 角色行 `content` 长度常为 0（实际文本在 `tool_calls`/`reasoning`/`api_content` 列）——勿仅凭 assistant 行 `length(content)=0` 判定「消息为空」
- **`sessions` 表按 `id` 列查，没有 `session_id` 列**（`session_id` 是 `messages` 表的外键列名），**也没有 `name` 列**（显示名是 `display_name`）——写 `SELECT session_id FROM sessions`、`SELECT name FROM sessions` 直接报 `no such column`；`messages` 表也没有 `folder` 等未文档化列。写多段 SQL 前先 `PRAGMA table_info(<表>)` 或 `SELECT * LIMIT 1` 核对列名再查，别凭印象拼列。
- **复用上一轮脚本前先核列名**：接力/探针类任务每轮爱 `cp` 改上轮的 verify 脚本，旧脚本的 SELECT 列表随时可能踩已不存在的列（实测复制旧轮脚本跑 `SELECT name FROM sessions` 当场崩、后续查询全部没跑到）——移植脚本先 `PRAGMA table_info` 核对每个被查表的列，或用本技能 scripts/ 下的现成脚本，别整段照抄。
- **容器内常无 sqlite3 CLI**——一律 python3 内置 sqlite3 模块连库（`sqlite3 --version` 查无时不要去找 CLI，直接写 python）。
- **日期字符串 `LIKE '2026-10-29%'` 对 epoch 列恒 0 行（假阴性，不报错）**：`started_at`/`timestamp` 存 float 秒，按 ISO 日期字符串过滤一条都查不到且无任何异常——按天开窗先算 epoch 秒区间，`cast(<列> as real) BETWEEN <起> AND <止>`；session id 前缀 `20261029_` 可作辅助筛但排序/窗口仍走列
- 连接用 `sqlite3.connect(path)` 即可（WAL 模式，读方不持排他锁）；`messages` 表 `role='session_meta'` 行 content 可为 NULL，过滤时显式写 `role='user'` 或 `role IN ('user','assistant','tool')`

## 变更审查（后台 curator 改动核对）

本技能会被后台 curator 自动改写（升版本 + 加 scripts/），改完不通知、只挂一个「待审查」标记。拿到该标记时按四步核实，**不要因为是自己技能就默认已验过**：

1. **找改动人**：从标记时间倒推 `agent.log` 里的 `origin=background_review` turn 与随后的 `tool_executor` 记录，可确认是 curator 而非主人指令。工具调用时间戳与文件 mtime 对得上才可信。
2. **看变更面**：`ls` 技能目录，比对 SKILL.md mtime 与 `scripts/` 下新增文件——curator 习惯「加一个脚本 + SKILL.md 加一节指路 + 版本号升一位」，三件套齐了才算一次完整改动。
3. **验新增脚本的自我声明**：脚本注释声称「只读」就得实证——`grep` 写操作关键字（DELETE/UPDATE/INSERT/DROP/commit）确认零命中，并确认连接串真的是只读 URI（`sqlite3.connect("file:...?mode=ro", uri=True)`）；光有注释不算数。再跑 `py_compile` 过语法。
4. **对变更说明与版本历史条目**：脚本实际能力必须与 SKILL.md 版本历史描述逐条对得上，对不上说明描述有夸大或实现有漏。

5. **自己改完也要查一遍并发写入（已踩坑）**：后台 curator 与前台会话可能**同时改同一个文件**。改前记下 `ls -la --time-style=full-iso` 与 md5，改完重新 `md5sum` 比对——mtime/md5 在你编辑期间变了就是有并发写入，**必须重新 `skill_view` 读回全文**，把对方刚落的规则与自己的逐条对齐。本技能已发生：改完锚点口径后，curator 并行会话又落了一条**不同且与代码矛盾**的锚点规则，以及一个**占掉同一版本号**的条目。两条教训：**版本号新 ≠ 内容一致**（别拿版本号当一致性证据）；**批量 patch 中途失败要按已完成数重算**，已落地的 op 可能随后被并发写覆盖（本技能实测 5 个 op 里 2 个被回退），失败后必须重读全文再逐条重放，不能假定前 3 个还活着。
6. **同一主题只留一处权威口径**：发现同一规则在多处各有一份表述时，保留一处完整的、其余改成指向它，并显式写明被否定的变体错在哪——否则下一个执行者（或 curator）会挑中错的那份。

**已知限制**：curator 改完本技能后，试图同步更新 `skill-library-maintenance` 的版本注册表会被拒（该技能 user-owned，`created_by=None`）。后果是新版本**无注册表登记**，主人定的「更新必登记」规则对这批自动改动不生效——需主人在前台会话 `hermes curator adopt` 后补登。遇到未登记的新版本要主动提醒。

## 版本历史

> 全量历史见 `references/changelog.md`；版本登记以 skill-library-maintenance 注册表为准。
> 最近变更：
- v1.23.2 (2026-10-29)：SQL 陷阱补「日期字符串 LIKE 对 epoch 列恒 0 行」——`started_at`/`timestamp` 存 float 秒，按 ISO 日期字符串过滤不报错但一条都不命中，追写入者/当日会话时极易误判「库里没有」；按天开窗先算 epoch 秒区间再 `cast(<列> as real) BETWEEN <起> AND <止>`。
- v1.23.0 (2026-10-21)：731370 轮接力实测纠错——**hidden 行数 ≠ 灰条实证**。逐行分类生产数据发现 `display_kind='hidden'` 全是打断占位（`[interrupt` / `response interrupted` / 空 content），而 `_compressed_summary=1` 件 display_kind 全为 None（merge-into-tail 分支），故「hidden 有行」推不出「灰条发生过」；灰条唯一样本判据是 `display_kind='hidden' AND _compressed_summary=1`，为 0 时 UI 项结论只能写「未验证/能力边界」（前轮据此写「生产实证存在」已在本轮报告内更正）。`scripts/verify_relay_chain.py` 末段相应重写（存量统计 + hidden 构成分类 + standalone 样本判定），SKILL.md 脚本说明加铁律，references/relay-handoff-e2e.md 取证第 4 步同改。
- v1.22.1 (2026-10-21)：落库守卫 round 10（ms=1792531974229）复证——v1.22.0 模式继续成立：探针承载 session `20261021_053256_c48856`（source='tui'、title='首条消息落库守卫 round 10 1792531974229' 精确命中，started_at=探针+22.4s），user 行独立落库（id=91739、content 精确=探针原文）；同窗兄弟轮 round 8/11 user 行各 1、round 12 随后 +65.3s 落库，通道健康正向对照；锚点无漂移，判定走优先级 1「已落库」。
- v1.23.1 (2026-10-25)：changelog 瘦身——全量历史迁 `references/changelog.md`，本节只留最近两条（正文与 references 已承载全部判据，抽查 8 项确认）。
