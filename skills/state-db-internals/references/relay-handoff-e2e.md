# E2E 接力 / 交接摘要链路验证（UI-E2E-SUMMARY-MARK 类）

## 何时使用
- 入站 user 消息形如「【交接摘要】…目标…下一步…UI-E2E-原话-NNNNNN」或 session 标题形如「接力UI源-NNNNNN-b」——即 E2E 接力测试的 B 会话（被续接方），任务多为「验证灰条与 A 历史拼接（UI-E2E-SUMMARY-MARK）」。
- 需要还原**已被清理的 A 会话**（一次性接力源会话）的入站原文与处置结论时。

## 链路模型（先建立这个心智模型，再动手）
一次接力周期 = A 会话（一次性，标题「接力UI源-NNNNNN」）→ 交接摘要（UI 侧称「灰条」）→ B 会话（标题「接力UI源-NNNNNN-b」）。
- A 会话**交接后即注销**：state.db 里按 A 的 session id 查无是**预期链路行为**，不是记录丢失、不是落库失败——不得据此判「请求未送达」或「被删故障」。
- 摘要三要素：`## 目标 / ## 下一步 / 原话标记`（逐字保留，收口报告里要原样回引）。
- 「拼接」的机制：A 历史以交接摘要 user 行拼接入 B（B 首条 user 行 content 即摘要全文，display_kind 为普通用户行）；standalone 交接摘要另在 UI 侧走 hidden 投影（`agent/session_persistence.py:_summary_display_kind` + ui-tui `domain/messages.ts`：display_kind='hidden' 的件整条跳过、不占活跃用户槽）。A 会话内容由摘要承载，不再单独可见。
- **多周期串联是常态，不是单跳**：A 交接后 B 续接，B 收口时再产出摘要成为下一周期的源——链上每个 B 的首条 user 行都是上一轮摘要。验链式拼接用「全链一致性核对」（取证三步第 3 步），只验当前这一个 B 会漏掉更早轮次的漂移。

## 取证三步（载体在日志与 DB，不在 /opt/data 文件系统）
1. **还原 A 会话入站原文**：在 agent.log 检索 `agent.turn_context: ... history=0 msg='<原文>'` 行；A 的回复看同 session 的 `agent.conversation_loop: Turn ended: reason=... response_len=N` 行（A 已注销后这是唯一可回引的原文来源）。
2. **验 B 会话拼接**：state.db `SELECT id,role,substr(content,1,120) FROM messages WHERE session_id='<B id>' ORDER BY timestamp LIMIT 1`——首条 user 行应为交接摘要，三要素无缺漏即拼接生效；同时 `agent.turn_context` 的 `history=0` 坐实「入站即摘要、无前置历史」。
3. **链式全链一致性核对（≥2 轮接力时必做）**：`SELECT id,title,started_at FROM sessions WHERE title LIKE '%接力UI源%' ORDER BY started_at` 列出全链（**当前会话自己也在链上，别当异常剔除**）。⚠️ 接力会话名在 `title` 列，api_server 会话 `display_name` 为 None——按 `display_name LIKE '%接力UI源%'` 查恒 0 命中，别据此判「链上无会话」。⚠️ 该 LIKE 同时命中 A 会话（标题无 `-b` 后缀，首条 user 行是该周期测试方下发的一句话指令，无三要素、无原话标记）与 B 会话（`-b` 后缀）——sha256 比对与标记收集只覆盖各 **B** 会话，把 A 混进比对会因「无目标字段」误判漂移；对每条链上 session 的首条 user 消息提取 `## 目标` 字段做 sha256 比对——全链同哈希 = 拼接零丢字；每轮原话标记（`UI-E2E-原话-NNNNNN` 尾行）必须各不相同，出现重复即串轮。读链上更早 B 会话的完整 tool_calls 序列，能直接看到前几轮做过的死路——先扫它再决定本轮查什么。
4. **验灰条语义**（只在需要解释 UI 表现时）：读 `session_persistence._summary_display_kind` 与 ui-tui `domain/messages.ts` 的 display_kind 分支，引「hidden 件不占活跃用户槽」即可，不必展开全链。**取证分层——先分类，再结论**：
   - hidden 行的主体是打断占位（`api_content` 以 `[interrupt` / `[response interrupted` 开头，或 content 为空），**不是**压缩摘要件；
   - `_compressed_summary=1` 的件在生产中 `display_kind=None`（走 merge-into-tail、保留原可见性），**从来不会**落到 hidden 分支；
   - 因此「hidden 有行」推不出「灰条发生过」，两者都不是灰条实证。**灰条唯一样本判据 = `display_kind='hidden' AND _compressed_summary=1`**（只有 standalone 交接摘要才被置 hidden）；该数为 0 时，UI 项结论只能写「未验证/能力边界」，并在报告里写清「机制代码路径在位、生产零样本」。曾据 hidden 行数写过「生产实证存在」，属于把占位件当灰条，后续报告必须主动更正而不是跟着转引。
   - 查生产实证要**精确过滤**：用开头锚定 `content LIKE '[CONTEXT COMPACTION%'` 或 `_compressed_summary=1` 列；粗 `LIKE '%CONTEXT COMPACTION%'` 会把**本轮自己检索类工具输出的回显**误算成生产件，计数前必须按 `timestamp < 本轮首查时刻` 剔除自产行。
   - **计数必须声明 active 口径且只当存量看**：`active=1` 与全量差数倍，轮间计数还会随压缩/打断自然波动（同一库数小时 78 → 117）——跨轮比数没有必要，比「分类构成与 standalone 样本数」才有意义。无论生产实证如何，本周期结论以该周期 B 首条 user 行 `display_kind=None` 为准——接力摘要走普通用户消息通道，不触发灰条。

## 已知工具落差
- `session_search` 的 browse 模式**仍会列出已注销的 A 会话**（索引滞后于删除），但按 `session_id=` 读同一会话报 `session_id not found`。**browse 命中 ≠ 可读**——证据一律以 state.db 与 agent.log 为准，别在 session_search 上反复重试。

## 收口报告格式（接力类任务的交付物）
按交接要求输出结构化报告，四段：**目标**（复述 + 点名 SUMMARY-MARK 类测试名）/ **已定的决定**（原话标记逐字保留、链路模型一句话）/ **验证结果**（A 原文、B 摘要三要素、灰条机制各一句证据）/ **链路结论**。UI 类项附边界声明一句：代码路径已核实 + 未目视确认 + 人工复核入口。多条接力周期并存时，前周期已卡壳的要在报告里点名「翻查无产物」，本轮直接收口。

## 坑
- **禁止全盘翻文件与代码找 E2E 标记**：`752505`、`SUMMARY-MARK`、`UI-E2E` 在 /opt/data 文件系统和 /opt/hermes 源码树里均恒零命中，翻查只烧轮次不产证据——载体在 agent.log 与 state.db（多个周期实测均卡于此）。
- **验「UI 呈现」类断言前先判线上部署的是哪套 UI**：经典 React WebUI 与 ui-tui 并存且 dist 构建物只在一侧（`ls /opt/hermes/web/dist` 空、`/opt/hermes/ui-tui/dist/entry.js` 在库 → 线上是 ui-tui）——给没部署的那套读渲染代码=答非所问。且 agent 位于网关之后**无法直接截图界面**：UI 类测试项只能代码路径取证 + 收口报告如实写「未目视确认」并给人工复核入口，**不许把「渲染代码存在」写成「界面已验证」**。
- **「UI-E2E / 接力 / 灰条」字样不触发 probe-batch-verification**：该技能只在主人给出探针批次标记或 probe 附件时用；workspace 里的 probe 批次文件与 E2E 接力测试无关，别当 E2E 载荷核验。
- **别把 A 会话的注销判成链路故障**：交接后清理是设计行为；报告口径写「一次性会话，交接后即清理，属预期链路」。注销是**分钟级且分两阶段**的：先清 `messages` 行（此时 `sessions` 行仍在、`message_count` 留陈旧值，如 mc=2 而 messages=0），**随后 `sessions` 行本身也被清**（实测终态两行皆无）。**两阶段间隔不固定**——572889 轮实测 B 会话创建后数分钟内 `sessions` 行仍在、再查才消失：中途查到 A 行在不是异常，也别据此写「A 未注销」。复查 A 时查无消息行先核 `sessions` 行在否；若连 `sessions` 行也没了，照样是注销的终态而非故障，「查无 A」在任一阶段都不得改判「被删故障」。**messages 行清理也可能滞后于 B 会话创建**：交接完成的瞬间查 A 仍可能有 2 行（user+assistant），数十秒内自动清零（548346 轮实测：首查 messages=2、隔约一分钟复检=0）——首次查到 A 消息行还在不是异常，判「清理未执行 / 注销异常」前稍后复检一次再下结论。
- **每轮核对不要手拼 SQL，用现成脚本**：`scripts/verify_relay_chain.py <B链id...> [--a <A id>]`（SKILL.md 一键取证脚本节有用法）一次跑完全链三要素 / sha256 / 标记互异 / display_kind / A 注销状态 / 灰条样本判定；尤其别照抄上一轮的 verify 脚本——`sessions` 表没有 `name`/`session_id` 列，旧脚本的 SELECT 列表会当场报 `no such column` 且让后续查询全部落空（548346 轮实测踩中）。复制冲动有两个稳定信号：/opt/data/tmp/ 里躺着历轮 verify_<标记>.py、且交接摘要写着「继续做完」——同时见到就切到技能脚本。
- **前轮报告的结论要复核，不能沿引**：同一接力链跑几十轮，每轮报告都抄上一轮的判断句，会把一次误读固化成年年重复的「历史结论」。凡是引用往轮结论的地方（尤其 UI/灰条、生产实证、注销状态这类只能间接取证的项）本轮都要拿原始证据重算一遭；发现前轮说过头，本轮报告显式写「纠正上轮结论」+ 原始分类数据，而不是默默改口径或跟着转引。
- **多周期并存时逐周期独立收口**：每条交接摘要有自己的原话标记，报告只回引本周期那个，不借用邻周期的结论顶替。**接力节奏可能不断加快**（实测周期间隔从约 80 分钟缩至约 80 秒），后继周期可先于本轮入站、甚至先于本轮落报告——这是链路正常运转的佐证不是干扰，全链核对自然会多出周期号之外的 B 会话，报告点名「后继周期已自行收口」即可。
- **上轮报告的链列表会过时——全链 B 列表必须实时发现**：新接力周期可在上轮报告签发前后重叠启动（上轮 B 还在写收口报告时，新 A/B 已入站，实测上轮报告 7 轮链签发后即冒出第 8 轮），抄上轮报告的链会漏掉已发生的周期。每轮全链核对按 `SELECT id,title FROM sessions WHERE title LIKE '%接力UI源%-b' ORDER BY started_at` 实时取；**发现无独立报告留档的周期不等于该周期卡壳**——其 B 会话数据在库完整（首条 user 行=摘要、标记在列），纳入本轮全链 sha256/标记互异复核，并在报告链路结论里点名「该周期无独立报告、数据在库可证」。**影子周期是驱动自愈行为，不是链断**：agent.log 里可能出现上轮报告未登记的原话标记（同一条 msg='请只回复两个字：收到' 再次入站），其 B 会话 API 调用与收尾都正常（text_response）却未落报告、且 messages 与 sessions 行双清在库无痕——这是驱动对该周期判定失败后的重发。判读口径：authoritative 链 = state.db 中首条 active user 行为交接摘要的会话；日志有标记而库里无对应链行 = 影子周期，在报告「附带观察」点名旁支即可，**不得当成链断裂去补周期**；另一面，最早的周期可能本就没有报告落档（当时以会话内回复收口），无文件同样不等于卡壳。
- **`run_agent` 初始化日志的 session 标签会滞后**：`POST /p/default/api/sessions` 201 创建新会话后，紧接的 `run_agent: OpenAI client created (agent_init, shared=True)`、一批 `tools.registry: check_fn ...`、`tool_search activated` 初始化行，标签仍打在上一个会话 id 上（日志上下文要等新会话的 `conversation turn:` 行出现才切换）。看到已收尾的会话名下冒出 run_agent + tool_search 却没有 `conversation turn` 行时，先看前一行是不是 `POST .../api/sessions HTTP/1.1" 201`——是则这批初始化属于刚创建的新会话，不是老会话的第二轮，勿据此判「A 会话被二次唤醒」。
- **环境便签不在摘要消息行里，别拿 DB content 判「便签丢失」**：B 首条 user 行 content 就是交接摘要全文（Python len() 约 77 字符 / UTF-8 约 133 字节，三要素即全部内容）；环境便签（env-reminder）是 pre_llm_call 当轮注入 prompt、**不写入消息行**——判「便签是否透传」要看当轮实际收到的注入内容，对 DB 首条 user 行 `LIKE '%记忆·星光%'` 得 0 是常态而非丢件，同理不得要求摘要 content 里含便签字段。
