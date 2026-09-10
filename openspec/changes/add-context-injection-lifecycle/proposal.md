## Why

`add-itemized-rollout-context` 已经定义 canonical item、ContextRequestPlan、assembly 和 rewind/compaction 的基础合同，但 AGENTS、Skill、团队状态与 checkpoint reminder 仍由不同 producer 直接拼接消息，既无法保证已提交上下文前缀的字节级稳定，也缺少统一的 source revision、追踪和恢复语义。

本 change 将所有上下文变更收敛到同一个 Saver/ContextStore mutation owner，同时只由 Context Source Manager（CSM）管理需要 revision、追踪和恢复的 source lifecycle，并把稳定前缀、Skill 加载模式、ToolSet hard rebase 和 wire role 规则固化为可验证合同。

## What Changes

- 新增统一的上下文 mutation 边界：真实用户消息、assistant/reasoning、tool call/result 通过 canonical append intent 提交；source lifecycle、ToolSet switch、compaction/rewind rebuild 使用各自语义明确的 intent，但全部由同一个 Saver/ContextStore owner 原子提交和 seal。
- 该 owner 的最小生命周期单位是 `SessionThread` 而非产品 Session：Session 只保存唯一 main thread、child thread catalog 与共享资源；每个 thread 独立拥有 canonical context、CSM state、active view、prefix epoch、ToolSet applied binding 和 sealed assembly。LangGraph `checkpoint_ns` 仍仅为 thread 内子图 namespace，不得成为 CSM/ContextStore owner key。
- 固定 thread owner 与外部附件正文的物理边界：main thread 位于 session node 的 `threads/{main_thread_id}`，其它 durable thread 位于按不可变 UTC 创建日期和稳定 hash shard 分桶的路径；附件正文统一位于 workspace `.boxteam/attachments/` 内容寻址 store。CSM/ContextStore 只持久化逻辑 attachment reference、owner/view membership 和 provenance，不把物理 blob path 注入模型上下文或 source detail。
- 新增 `ContextSourceManager`（CSM）作为该 owner 下的 source lifecycle 子管理器，负责 AGENTS、Skill、团队角色/任务状态及其它动态 source 的 identity、revision、diff、追踪状态和 reconciliation；它不接管普通 canonical append，也不维护注入次数。
- 将“已提交上下文前缀字节级稳定”设为首要约束：同一 `prefix_epoch` 内的后续请求只能在已提交 wire context 后追加新 item，不得回写、合并、重排或重新序列化旧 item；合法 epoch 边界只有首次组装、实际 compaction、rewind 和 ToolSet hard rebase。
- 将所有有效 ToolSet 变化定义为 hard rebase：当前 in-flight sealed model call 保持不可变，desired revision 在下一个 model-call safe boundary 生效，先真实收敛旧工具调用，再封存新 ToolSetSnapshot/Ref、创建 `epoch_reason=toolset_changed` 的新 prefix epoch 并重建 root/messages/tools 投影；禁止用 user-role 文本模拟软切换。
- 首次组装只生成一个最顶层 `wire_role=system` root item；第一条真实用户消息之后追加的 CSM 完整内容、delta 及 rewind/compaction 恢复内容当前统一使用 `wire_role=user`。
- 为 Anthropic 官方部分模型未来可能支持的中途 system item 仅保留 Provider capability TODO；当前运行时不得启用该分支，也不得因此改变通用 role 合同。
- 将一个 `SKILL.md` 拆为相互独立的 Skill metadata 与 Skill activation source；metadata 当前只读取 `name` 和 `description`。
- 新增仅按名称调用的 `skill_load(name, mode="snapshot" | "tracked" | "untrack")` 工具，默认 `snapshot`；模型不可读取或传入 Skill 路径。
- `snapshot` 稳定读取一次当前 `SKILL.md` 并追加一个不可变 activation item，之后不检查文件变化，也不在 rewind 移除后自动恢复。
- `tracked` 由 CSM 保存内部 source identity/path、revision/hash 和追踪状态；每次模型请求前检查变化，把相对最新已提交且仍可见 revision 的 diff 追加到尾部，并在有效 registration 被 rewind 保留但最新注入移出 active view 时追加当前完整 revision。
- `untrack` 仅停止 CSM 后续检查与自动恢复，不删除、改写或立即移除已经进入上下文的 item；本 change 不提供 Skill 即时移除工具。
- 多次尚未进入 sealed assembly 的 tracked 变化合并为一个从最新已提交可见 revision 到当前 revision 的 delta。
- 增加 `${BOXTEAM_HOME}/skills/` 的 Gateway 全局 Skill catalog，并与 bundled、workspace Skill 形成确定性名称解析；Gateway 不写工作区 Session/CSM 状态。
- 将团队状态和其它内部通知迁移为 ambient/pending runtime source item；`wire_role=user` 只是 Provider 投影，不得创建真实用户 Turn root。
- 建立现有生产上下文来源的迁移闭包：覆盖 Agent 配置说明、运行时身份、Todo/Filesystem/Skill/AGENTS/压缩/Memory middleware、Goal/委派/跨会话/团队/终端/retry/checkpoint 事件，并逐项指定 CSM、ToolSet、compaction 或 canonical Turn owner。
- **BREAKING** 删除 `PromptReplayCaptureMiddleware`、捕获标签和 middleware 间 prompt diff 链路；不保留诊断兼容模式，所有生产 instruction/runtime producer 必须在 assembly seal 前显式登记。
- **BREAKING** 废弃 `ItemizedContextProjectionMiddleware` 从已组装 LangChain request 反向捕获 prompt/tool/context 的实现。框架若必须使用 model-call middleware 钩子，则将该接入点改造成无状态 sealed-assembly dispatch bridge；框架不需要时直接由 Provider dispatch adapter消费 sealed assembly。
- **BREAKING** 删除生产路径中由 middleware 通过通用 read 工具加载 Skill、直接追加内部 `HumanMessage`，以及通过可变有效期或计数改写 active context 的实现。

## Capabilities

### New Capabilities

- `context-injection-lifecycle`: 定义稳定前缀、CSM source revision/追踪生命周期、Skill catalog 与 `skill_load`、wire role、rewind/compaction 恢复和 sealed assembly 投影合同。

### Modified Capabilities

本 change 通过显式集成合同复用 `add-itemized-rollout-context` 已规划的能力，不直接修改或复制其 requirement。

## Impact

- 影响 `app/agents/agent_factory.py`、`deep_agent_stack.py`、`middleware_prompts.py`、`skill_runtime.py`、压缩与 memory middleware、内部 structured prompt producer；删除 `PromptReplayCaptureMiddleware`，并删除或重构 `ItemizedContextProjectionMiddleware` 为无状态 sealed-assembly dispatch bridge。
- 影响 checkpoint/context owner、source reconciliation、assembly manifest/detail store 和 Provider projector：owner 需要接受 canonical append、source lifecycle、ToolSet switch 与 epoch rebuild 四类 mutation intent；dispatch 只能消费 Saver-issued sealed assembly reference，不能从 framework request反向生成 contribution、ToolSetSnapshot或 assembly。
- 影响 SessionThread catalog、thread-qualified storage/checkpoint config 和 GraphBinding 重建：CSM source identity、tracking registration、ToolSet applied revision 与 sealed dispatch reference 必须绑定 `(session_id, thread_id)`；Session main-thread 默认路由不能被内部 producer 当作跨 thread fallback。
- 影响 attachment ingress、canonical append、Provider projection 与清理流程：需要 workspace attachment catalog、按内容去重、session/thread/item reference、capability 校验和引用感知 GC；CSM、history 和模型工具不得扫描 `.boxteam/attachments` 或暴露其 locator。
- 影响 ToolSelectionStore、ToolService、执行 step/Agent 生命周期和 ToolSet registry：需要区分 desired/applied ToolSet revision，在每次 model call 前的安全边界检测变化，并以 hard rebase替代“运行中 Job 永久沿用旧 Agent/工具集”的隐式行为。
- 影响 Goal、subagent/session generation、跨会话消息、团队、终端 steering、execution retry 和 checkpoint reminder 的派发方式；这些内部事件需要独立于真实用户 acceptance/Turn root 的 execution wakeup。
- 影响 Gateway 全局 Skill catalog、`${BOXTEAM_HOME}/skills/` 与 workspace/bundled Skill 名称解析，但 Gateway 仍不得读写工作区 `.boxteam/` Session 数据。
- 需要增加 CSM 追踪控制状态、source revision/detail、稳定前缀 epoch/reason/hash/length、desired/applied ToolSet revision、Skill metadata/activation provenance 及 `skill_load` 工具 schema。
- 需要更新 Python 单元/集成测试、真实 Provider request projection、Web/tool-loop 回归及 snapshot/tracked/untrack、rewind、compaction、restart 场景。
- 本 change 只修订规划；实现阶段继续由单一 Saver/ContextStore owner 持久化，不建立第二个 SQLite/JSONL writer。
