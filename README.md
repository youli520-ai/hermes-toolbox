# hermes-toolbox

**Hermes Agent 的实战沉淀：流程治理技能 + 网关插件（快路径 / 环境便签）。**

by [youli520-ai](https://github.com/youli520-ai)

从单 owner + 多 agent 并行开发的真实环境里提炼出来、经过日常使用与多轮对抗审查的
一套轻量流程治理资产。与 [spec-pipeline](https://github.com/youli520-ai/spec-pipeline)
同源同哲学：**状态机外化、机器拦动作、人判质量、失败路径优先**。

## 目录

```
skills/     8 个技能（SKILL.md + references/templates）
plugins/    2 个网关插件（0 token 快路径 / 环境便签注入）
```

## 内容与版本快照

| 类型 | 名称 | 版本 | 一句话 |
|---|---|---|---|
| skill | todo-resume | 2.4.1 | 待办续接体系：中文 md 建档 + 面板 + cron 双层绑定（档案=事实源，cron=唤醒器） |
| skill | skill-review-checklist | 1.23.2 | 技能逐字审查：八维快速扫 + 实跑验证 + 盲区表，含大量实测教训 |
| skill | script-dev-discipline | 1.17.0 | 跨平台脚本开发规范：环境确认、模板复用、静态校验、3 轮迭代终止 |
| skill | state-db-internals | 1.23.2 | Hermes state.db 内部结构与验证姿势（落库守卫、会话指纹） |
| skill | verification-before-completion | 1.1.0 | 宣称完成前必须跑验证命令，写入 ≠ 生效 ≠ 可用 |
| skill | writing-skills | 1.0.1 | 写技能的方法论：context pointer 与双负载，解决规则不生效 |
| skill | using-superpowers | 1.0.0 | 会话开始时如何查找与使用技能 |
| skill | disk-space-triage | 1.8.0 | 磁盘/存储告警定位：容器内外可回收项分清、清理方案先行 |
| plugin | fast-command | 1.1.0 | 0 token 快路径：消息进网关先过确定性指令（面板直发/待办序号查询/高危拦截），不唤醒 agent |
| plugin | env-reminder | 0.4.2 | 环境便签：采集脚本每 5 分钟注入环境状态到当轮消息尾部，agent 天然可见 |

## 安装

```bash
# 技能：拷到你的 Hermes profile 技能目录
cp -r skills/<name> ~/.hermes/skills/

# 插件：拷到网关插件目录，重启网关生效
cp -r plugins/<name> ~/.hermes/plugins/
```

### fast-command 配置说明

`config.json` 关键字段：

- `owner_user_id` / `allowed_chats`：只对 owner 的会话生效（换成你的 open_id / chat_id）
- `high_risk_commands` / `high_risk_regex`：高危指令直接拒绝快路径、交给 agent 二次确认
- `fast_script_allow`：script 型指令白名单（默认拦截，白名单内放行）
- `todo_query_words`：待办序号查询的词表（配置驱动，改 JSON 即生效）

指令库引擎（script 型指令库精确匹配）需要配套的 `command_lib` 模块 + 库 JSON，
通过环境变量 `FAST_COMMAND_LIB_FILE` 指定路径；未配置时该引擎自动禁用，其余功能不受影响。

### env-reminder 配置说明

需要一个 5 分钟级的环境采集脚本（输出 JSON delta 到 `delta_path`），
插件负责把增量渲染成便签注入当轮消息。监控键与展示名在 `__init__.py` 的
`_LABELS` / `_PROFILE_CN` 里按需改。

## 诚实的边界

- 这里发布的是**快照**：上游继续演化，仓库不承诺自动同步。
- 内容已做脱敏（主机名、账号 ID、专属实体全部泛化或替换为示例），示例表里的
  「主机A/B / profile-A/B」是占位，不是可运行的配置。
- 技能里的 changelog 保留了演进史（怎么踩坑、怎么修），比结论更有价值的往往是过程。
- 插件按 Hermes 网关插件接口实现；不同版本接口可能变动，以你本地网关为准。

## License

[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) —— 署名即可：注明
作者 `youli520-ai` 并链接本仓库。

Copyright (c) 2026 youli520-ai
