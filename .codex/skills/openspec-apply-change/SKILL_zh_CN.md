---
name: openspec-apply-change
description: 实施 OpenSpec 变更中的任务。用户希望开始实施、继续实施或逐项完成任务，并希望使用中文工作流时使用。
allowed-tools: Bash(openspec:*)
license: MIT
metadata:
  author: openspec
  version: "1.0-zh-cn"
---

# 实施 OpenSpec 变更

从 OpenSpec 变更中读取规划上下文并实施任务。

**Store 选择：**如果用户指定 store，或工作位于已注册的独立 OpenSpec 仓库中，先运行 `openspec store list --json`，然后为读写 spec 与 change 的命令附加 `--store <id>`。命令提示已带此参数时，后续命令继续保留。未指定 store 时，命令作用于最近的本地 `openspec/` 根目录。

**输入：**可选的变更名称。省略时可从对话推断；含糊或有多个候选项时，必须列出变更并让用户选择。

## 步骤

1. 选择变更。名称明确时直接使用；只有一个活跃变更时可自动选择；否则运行 `openspec list --json` 并询问用户。始终说明“正在使用变更：<name>”以及如何改选。
2. 运行 `openspec status --change "<name>" --json`，读取 `schemaName`、`planningHome`、`changeRoot`、`actionContext` 及任务 artifact 的位置。
3. 运行 `openspec instructions apply --change "<name>" --json`。解析 `contextFiles`、进度、任务状态和动态指令。
   - `state: "blocked"`：说明缺失 artifact，建议使用 `openspec-continue-change`。
   - `state: "all_done"`：说明已完成并建议归档。
   - 其他状态：继续实施。
4. 完整读取 `contextFiles` 中列出的每个文件。不要假定 artifact 文件名；以 CLI 输出为准。
5. 展示 schema、`N/M` 进度、剩余任务概览和 CLI 动态指令。
6. 逐项实施未完成任务，保持改动最小且聚焦；完成后立即把任务文件中的 `- [ ]` 改为 `- [x]`。
7. 任务含糊、发现设计问题、出现错误或阻塞、用户打断时暂停并说明原因，不得猜测。
8. 完成或暂停时展示本次完成项、总体进度及下一步建议；全部完成时建议归档。

## 输出格式

实施中说明当前变更、schema、正在处理的任务编号和结果。完成时列出本次完成的任务及总体进度；暂停时列出问题、可选处理方式并等待用户决定。

## 约束

- 开始前必须读取 apply 指令返回的全部上下文文件。
- 持续处理任务，直到全部完成或明确阻塞。
- 每完成一项立即更新复选框。
- 实施暴露规划问题时，暂停并建议更新 artifact。
- 不硬编码 artifact 文件名，只使用 `contextFiles`。
- 允许规划与实施交错进行，不把工作流机械地锁死在阶段中。
