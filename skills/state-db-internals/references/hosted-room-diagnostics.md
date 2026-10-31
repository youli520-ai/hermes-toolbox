# 官方房间（hosted room / Discussion）seq 与派发链取证

适用：诊断 bot_room 讨论房间的事件 seq 稳定性、派发链路（谁被派发、谁被取消、后继是否断裂、取消是否真生效）。与 `cross-profile-delivery.md` 分工：那边答「跨 profile 谁收到了」（各 profile state.db 的 messages 表），这边答「房间内部事件流与派发机制」（shared-state.db）。两边各查各的库，结论互不替代。

## 库位置（第一陷阱）

- **真正的房间数据在 `$HERMES_HOME/shared-state.db`**（default：`/home/agent/.hermes/shared-state.db`）。
- `state.db` 里同名的 `hosted_room_*` 系列表**实测恒为空（0 行）**——连 state.db 查房间会得到「无数据」假结论。**任何 hosted_room 查询先确认连的是 shared-state.db**。
- 容器内无 sqlite3 CLI：一律用 python3 内置 `sqlite3` 模块（脚本或 heredoc）。只读取证用 `sqlite3.connect("file:<path>?mode=ro", uri=True)`；WAL 库普通 connect 也不持排他锁。`created_at`/`updated_at` 等时间列为 float **秒**。
- shared-state.db 是**单库共享、不分 profile**：房间事件流可见所有成员的动作，但「某 profile 是否真的收到并处理」仍须回该 profile 的 state.db 查 messages，两库各答各的问题。

## 表速查（shared-state.db）

| 表 | 用途 | 关键列 |
|---|---|---|
| hosted_rooms | 房间元数据 | next_seq（下一个待分配 seq）、authority_epoch、disbanded_at |
| hosted_room_events | 事件流 | seq、kind、actor_json、payload_json（用户消息原文在 `payload.text`）、created_at |
| hosted_room_driver_tasks | 执行任务 | source_event_seq（触发事件）、status、cancel_id、execution_generation、terminal_at |
| hosted_room_policy_threads | 线程 | latest_user_seq、completed；thread_id 常等于 room_id |
| hosted_room_policy_watermarks | 每成员消费水位 | seen_through_seq |
| hosted_room_policy_cursors | 派发驱动水位 | through_seq、stopped_through_seq |
| hosted_room_policy_transcript | 落地转写 | seq、kind、settled_seq |

入口定位：按讨论编号/房间名找 room——`hosted_room_policy_threads WHERE thread_id LIKE '%<编号>%'`，或 `hosted_rooms WHERE name LIKE '%<房间名>%'`（name 即 Discussion 标题）。thread_id 在单线程房间里等于 room_id。
- **探针 ms 直接当键最快**：房间 id/名称常内嵌探针毫秒（如 `foldchk-1791675717230`）——`hosted_rooms WHERE room_id LIKE '%<ms>%' OR name LIKE '%<ms>%'` 一步定位，别从标题反推。
- **房间标题家族 ≠ 探针消息家族**：信封 `[Discussion: "…"]` 里是房间名，探针家族只写在 `payload.text` / state.db 的 user 行里；实测存在「标题是落库守卫某 round、正文却是另一家族探针」的房间。按标题挑家族会查错对象，家族名一律取**消息正文**。

## seq 稳定性判据（纯编号层，四查）

1. `SELECT seq FROM hosted_room_events WHERE room_id=? ORDER BY seq`——必须严格单调连续（1..N 无跳号、无重复）；
2. `hosted_rooms.next_seq == max(seq)+1`；
3. `authority_epoch` 全程一致——epoch 变化 = 权威网关切换，是 seq 重置/双写/幽灵事件的高危点；
4. events.created_at 随 seq 递增——倒挂 = 写入方时钟异常或并发乱序落库。

四查全过只说明**编号层稳**，不代表链路健康，必须继续查派发层。

**单轮结论不算稳**：行为确定性用**同结构双轮对照**验证——慢速间隔与快速突发各跑一轮（同为「三条用户消息→stop→disband」结构），两轮结构同构才判确定行为，单轮现象可能只是竞态抖动。

## 派发链判据（三层，逐层排）

1. **水印滞后**：`watermarks.seen_through_seq < threads.latest_user_seq` ⇒ 存在从未被任何执行方消费的用户消息——消息在库但没人处理过。差距越大越像派发入口断了。
2. **supersede 链断裂**：driver_tasks 中任务以 `superseded_by_newer_user_event` 被取消时，**必须存在 source_event_seq = 更新那条事件的后继任务**；只有取消、无后继 = 超越链断（典型成因：stop_requested / 停房抢在后继派发之前落库）。⚠️ **reason 字段是归因标签，不是触发者证据**：把该 `turn.cancelled` 事件的 `created_at` 与相邻事件对齐——若它紧跟 `room.stop_requested` 之后（亚秒级）而距最新 message.user 有数秒，实际触发者是 stop，reason 里的 supersede 标签不可直接采信。
3. **已取消仍在执行**：task status=cancelled、terminal_at 已落、房间已 disbanded，而 agent 仍拿着该任务 payload 在跑 ⇒ 取消只落了账、进程未被中断。**task status 不是进程存活的证据**：DB 只能证明「取消意图已登记」，判「真的停了」须核 worker 侧进程/日志。
4. **超时后迟到回复只落库、不进房**：task `status='failed'` 且 `cancel_id='deadline:1'` = Group Chat 回合超时（时长 = `terminal_at − created_at`，本机实测约 150 秒，现场相减为准、勿当固定常数）。**超时只终止派发、不杀 agent 进程**：该成员 session（`title='Group: <room_id>'`）继续跑到 `finish_reason='stop'`、终稿 assistant 行照常写进 state.db，但房间事件流不再出现它的 `message.member`，此后也无新 driver task ⇒ 产出只落库不发布。三证定案：①session 末条 assistant 行 `timestamp` 晚于房间末条事件 `created_at`（复查数分钟仍无对应事件）；②事件流末尾缺该成员的 message.member；③`terminal_at` 之后无 `source_event_seq` 相同的新任务。判读写「回复已落库但未进房」，**勿判「消息未送达」或「agent 没跑」**——两者都与库里已有终稿矛盾。对照：同窗对方成员任务 `status='settled'`（十几～几十秒完成）⇒ 超时源在本执行方耗时（模型/工具慢），不是派发链断。

辅助交叉：cursors.through_seq = 已处理到、stopped_through_seq = 因 stop 截断处，显示派发驱动消费到哪、在哪停住；turn.cancelled 事件的 seen_through_seq 是取消时刻现场快照，与水印表互相印证，两边打架时以时间戳晚者为最新事实。**控制事件（room.stop_requested）不进 policy_events、被 cursor 排除属正常设计，勿当「丢事件」**。

## 两个死胡同（勿浪费轮次）

- **turn_id 不含 room 维度**：不同房间可产生完全相同的 turn_id（形如 `d<epoch>.r<round>.p<?>.s<seq>.m<hash>`）；按 turn_id 全局去重/索引会跨房撞车，引用 turn_id 必须带 room_id。
- **日志侧零命中属正常**：hosted-room 驱动不把 room_id/task_id 写进 gateway.log/agent.log/errors.log，grep 这些 id 找不到任何行——协议层取证只认 DB，勿在日志上换关键词反复重试。

## 上报口径

- 编号层结论与派发层结论**分开给**：「seq 编号稳定（1..N 连续、epoch 无切换）」≠「派发链稳定」，混成一句会掩盖后两层的问题。
- 派发层问题给四件套：滞后水位值（seen/latest）、断链证据（有取消无后继的 task_id）、取消未生效证据（terminal_at 与当前仍在执行的事实）、超时证据（`deadline:1` 的 created_at→terminal_at 秒差 + 迟到终稿与房间末事件的时间差）。**每条缺陷给「现象 + 时间差数字 + 判读结论」**，数字取自事件时间线与任务面，不用记忆或日志推测。
- 只读取证，不做任何写库修复动作；修复须主人指令。
