---
name: team-collaboration-gpt
description: 本仓库 GPT 团队协作的轻量入口；使用 Luna 协调子代理管理核心 Sol 与辅助 Luna 孙代理，按需加载执行工具和当前任务记忆。
---

# GPT 团队协作入口

当前 GPT 协作采用 [分层团队技能](../team-collaboration-gpt-hierarchical/SKILL.md)：主线程最多直管 3 名 Luna 协调 child，唯一核心 Sol 和辅助 Luna 由协调层作为 grandchildren 管理。模型参数、派单、报告和等待方式只在该技能维护。

先读分层技能，不默认展开旧共同流程、全部失败历史或整份实时台账。交付协调者接续已有任务时，只读 [实时台账](memory/2026/10/10/team-collaboration-state.md) 的当前收口、租约与必要证据；主线程从其短摘要恢复。

现有 scripts/ 保持运行与交付工具的唯一实现。[共同流程](memory/2026/10/10/team-collaboration-workflow.md) 保留 DeepSeek 团队的调度说明及详细运行参数；分层 GPT 只按需读取执行、路径或核验章节，其调度角色以新技能为准。[链路核查规则](memory/2026/10/10/core-closure-rules.md) 仅在相应业务缺陷出现时读取。

只准备技能不启动业务团队；历史 goal 未闭事项不自动续派。
