---
name: openspec-archive-change
description: 归档已完成的 OpenSpec 变更。用户希望在实施完成后收尾并归档变更，且希望使用中文工作流时使用。
allowed-tools: Bash(openspec:*)
license: MIT
metadata:
  author: openspec
  version: "1.0-zh-cn"
---

# 归档 OpenSpec 变更

**Store 选择：**指定 store 时，先用 `openspec store list --json` 解析 ID，并为支持 store 的 spec/change 命令附加 `--store <id>`；否则使用最近的本地 `openspec/` 根目录。

**输入：**可选变更名称。未提供时运行 `openspec list --json`，仅展示活跃变更并让用户选择。不得猜测或自动选择。

## 步骤

1. 运行 `openspec status --change "<name>" --json`，读取 schema、路径上下文与 artifact 状态。
2. 若有 artifact 未达到 `done`，列出警告并请求用户确认是否继续。
3. 读取任务文件，统计 `- [ ]` 与 `- [x]`。有未完成任务时警告并确认；无任务文件则跳过。
4. 使用 `artifactPaths.specs.existingOutputPaths` 判断是否存在 delta spec。
   - 存在时，将每个 delta spec 与 `<planningHome.root>/openspec/specs/<capability>/spec.md` 比较，汇总新增、修改、删除和重命名。
   - 需要同步时提供“立即同步（推荐）”与“不同步直接归档”。已同步时提供“立即归档”“仍然同步”“取消”。
   - 用户选择同步时，遵循 `openspec-sync-specs` 的智能合并流程；选择取消则停止。
5. 确保 `<planningHome.changesDir>/archive` 存在，目标名称为 `YYYY-MM-DD-<change-name>`。目标已存在时明确失败，不得覆盖。
6. 将 `changeRoot` 移至归档目标，并保留随目录移动的 `.openspec.yaml`。
7. 展示变更名、schema、归档位置、spec 同步状态和所有未完成警告。

## 约束

- 未提供名称时始终让用户选择。
- 使用 `status --json` 的 artifact 图检查完成度。
- 未完成项只警告并确认，不强制阻止归档。
- 有 delta spec 时必须先完成同步评估并展示汇总。
- 不覆盖已有归档，不隐藏失败。
