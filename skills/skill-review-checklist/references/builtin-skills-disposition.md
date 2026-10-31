# 官方内置技能的处置：审，但不改

> 触发场景：审查一个命中 `.bundled_manifest` 的官方技能（hermes-agent、p5js、youtube-content 等），
> 或处理环境便签里的「技能审查：N 个待审查」名单时发现内置技能占位。
>
> 一句话：内置技能**照常审**（不漏问题），但**一律不改**（改了会被上游覆盖），
> 且**不进人工待办**（上游静默同步不该变成我们的人工负债）。

## 1. 判定归属

```bash
grep -c '"hermes-agent"' /home/agent/.hermes/skills/.bundled_manifest
# 命中 = 内置；未命中 = 本地/社区安装
```

- 只 grep，**不要写 JSON 解析**——`.bundled_manifest` 不是合法 JSON，`json.load` 抛异常
- 辅助信号（不单独定案）：frontmatter `author` 含 Hermes / Teknium / Nous
- 判不出归属 → **fail-closed 当内置**处理（宁可少打扰，不可误报），并保留只读查询入口兜底

## 2. 对内置技能做什么、不做什么

| 类别 | 动作 |
|------|------|
| 引用缺失 / 死链 | 只记录，不补 |
| 与我家环境脱节 / 规则冲突 | 写进自家技能（hermes-runtime-ops 等），不改它 |
| 凭据扫描误报（占位符、教学示例） | **修扫描器**，不改被误报的文档 |
| 疑似上游真 bug | 记备案，供主人择期上报 |
| frontmatter version 落后上游 | **不升**——上游一同步就被覆盖，本地升版本无意义 |

## 3. 为什么不给内置技能做 skip 名单

上游同步是静默的：只改文件 mtime，不留「谁改的」标记。硬编码 skip 名单会成为新的漂移源——
上游今晚加一个内置技能，名单没跟着变，它就又漏进 pending。

正确形态是**每次运行时读 manifest 判定归属**，让名单天然跟随上游。

## 4. done 时怎么写备注

```
--done "内置技能，只读审查；未改动本技能。
        问题已分流：误报→修扫描器；脱节→写入 <自家技能名>；疑似上游 bug→备案"
```

别写「已修复并升版本」——那不是事实，也会让下次误以为可以改它。

## 5. 待办分级（环境便签降噪）

- 本地技能待审 → 展示（现状行为）
- 内置技能有备案问题但无待办 → 不展示
- 主人想看内置备案 → `review_check --status --builtin`（只读）

## 6. 与其在机制上的落点

归属判定应修在最上游（collect.py 的 `iter_skill_files` / `detect_skill_changes` 分流），
**不要**在 `review_check.py` 下游过滤——否则 `--status` 输出的数字与环境便签的数字会对不上，
两处不一致比数字偏大更难排查。

红线「技能必审查」与此不冲突：内置技能仍跑自动审查（`--auto`），只是不占人工待办位。

## 7. 第三种归属：exempt（别的维护通道装的技能）

上游同步（builtin）不是唯一的「非我方变更」来源。**由家里 webui 侧安装/维护的技能**，mtime 变化同样不出自会话之手，却会被 pending 机制当成我们的变更。

- **与 builtin 的区别**：builtin = 上游同步，要备案（审计上游推了什么）；exempt = 别的维护通道，**无需备案**。混为一谈会让 `builtin-notes.md` 混进非上游变更，污染那条审计线索
- **落点**：`skill_origin.py` 的 `EXEMPT_FROM_REVIEW` 集合 + `origin_of()` 返回第三态；`collect.py` 对 exempt **既不挂 pending 也不写备案**
- **与「不做 skip 名单」的关系**：§3 反对的是给 **builtin** 做名单（上游会加新技能、名单必然过期）。exempt 名单相反——它**只能**是显式名单，因为它不是「跟随上游」而是「排除特定对象」，数量小、由主人点名，天然不该自动扩展
- **判据是「这次变更是否出自会话之手」，不是「内容归谁」**。加名前先确认该技能确实由别的通道维护，别把「我不熟」当豁免理由
- **⚠️ 加名要用 collect.py 实际产出的那个键，不是直觉里的短名**：插件路径经 `skill_name_from_path()` 产出的是 **`plugin:<名>` 带命名空间的键**（`plugins/video-ops/…` → `plugin:video-ops`）。写裸名（`video-ops`）会静默不生效——`origin_of()` 照样返回 `local`，pending 照挂，而名单「看起来已经有了」。加完必跑一句自验：
  ```bash
  python3 -c "import importlib.util as u; s=u.spec_from_file_location('so','/opt/data/scripts/skill_origin.py'); m=u.module_from_spec(s); s.loader.exec_module(m); print(m.origin_of('plugin:<名>'))"
  ```
  期望输出 `exempt`；再跑一次真实采集确认它不会被重新挂回 pending（**只在名单里写上不算验过**）
- 新增 exempt 后必须跑隔离测试：该技能不进 `to_pending`（含动了执行体的情形）、不触发备案、本地/内置行为无回归
- **若该技能同时是 dashboard 型插件（见 §8），两条路都通**：它本来就被 pending 机制误挂、又因无 SKILL.md 而审不了——加进 exempt 是正解（比手工审更省事），前提是确认它真由 webui 侧维护

## 8. 形态盲区：被 pending 标记却审不了的技能

`review_check.py` 定位技能依赖读 **SKILL.md / frontmatter**。**dashboard 型插件**（`~/.hermes/plugins/<名>/dashboard/`，内含 `manifest.json` + `api.py`，**无 SKILL.md、无 plugin.yaml**）因此会两处同时失效：

- 症状：环境便签与 `--status` 都列它为待审查，但 `review_check.py <名>` 与 `plugin:<名>` 三 profile 全报「❌ 技能未找到」——**待审清单清了不、自动项也跑不了**，靠现有工具无法闭环
- **处理**：不必等工具修好，**手工审**（它就是一份执行体），审完手工清 pending
- **手工清 pending 的具体做法（2026-10-18 实测，2026-10-30 两次修正——⚠️ 机制是两层，缺一即「清了又回来」或「刷了不消失」）**：①真跑一次 `review_check.py <名>` 与 `plugin:<名>`，确认确实是「技能未找到」而非别的问题；②**两层状态都要动**：A. 便签「待审查」的直接来源是 `/opt/data/state/skill_review_state.json` 的 `pending` 键（collect.py `read_pending()` 实读，12:46 实测只刷基线不删此键 → 便签照报）——审完删掉该键；B. `/opt/data/state/skill_scan_state.json` 的 `files` 基线（`{路径:{mtime,md5}}`）是变更检测层——把审过的文件条目刷成当前值（先核对三 profile md5 一致、属同一已审版本，连 profiles 侧一起刷），否则下轮变更检测重挂 pending（「删了又回来」的假象就是这么来的，⚠️ 因此判断「某法无效」前必须确认另一层也动过）；③向 `/opt/data/logs/skill_review.log` 追一行 `[YYYY-MM-DD HH:MM] 手工审查（dashboard 型插件，review_check 无法定位，按 checklist 盲区#17 手工清）` + 逐条结论（如：「无 subprocess/文件写/eval，全部委托 tools.write_approval」）；④收尾跑一遍真实采集（`python3 /opt/data/env_reminder/collect.py` 后 grep 确认该技能不再出现；`--status` 对 dashboard 型本就报未找到，验不出清没清）——**只改 state 不复跑等于没验**。
- **手工审查的对齐点**（比照 §2 的「审但不改」思路，但 dashboard 插件属自家可控，可改）：①读 `api.py` 的路径校验与写安全（realpath + 前缀匹配、输出撞号 409、并发锁、子进程参数用列表而非 shell）；②读 `manifest.json` 的 version 与能力描述是否与实现一致；③凭据不进代码（走加密库）；④版本注册表是技能侧的机制，dashboard 插件**不在注册表内**，见 §2「不适用」思路如实说明即可
- ⚠️ **别把「工具报未找到」写成「这个技能不存在」**：「未找到」= 工具覆盖不到这种形态，不是对象不存在。汇报时必须把两者分开，否则下个会话会去删一个真实存在的插件
- **主人说「排除」时先分清是排除哪种**：「这个排除掉」可能指「排除出待办清单」（→ 加 exempt，见 §7）也可能是「这次不审它」——两者落地动作完全不同。前者要改机制、后者只要不报。**审了也没意义的（如 webui 每次更新就回来的）应走 exempt**，让下个会话也不再被它打扰；只 skip 一次则下次 mtime 一动又回来

