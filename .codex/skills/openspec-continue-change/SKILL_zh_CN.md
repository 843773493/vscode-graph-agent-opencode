---
name: openspec-continue-change
description: 通过创建下一个 artifact 继续推进 OpenSpec 变更。用户希望继续现有变更、生成下一个规划产物或推进工作流，并希望使用中文时使用。
allowed-tools: Bash(openspec:*)
license: MIT
metadata:
  author: openspec
  version: "1.0-zh-cn"
---

# 继续 OpenSpec 变更

**Store 选择：**指定 store 时先解析 store ID，并为支持的命令附加 `--store <id>`；否则使用最近的本地 OpenSpec 根目录。

**输入：**可选变更名称。未提供时运行 `openspec list --json`，展示最近修改的 3–4 个变更，包括名称、schema、状态和 `lastModified`，将最近修改项标记为“推荐”，但必须由用户选择。

## 步骤

1. 运行 `openspec status --change "<name>" --json`，解析 `schemaName`、`artifacts`、`isComplete`、`planningHome`、`changeRoot`、`artifactPaths` 和 `actionContext`。
2. 若 `isComplete: true`，展示最终状态，建议实施或归档，然后停止。
3. 若有 `status: "ready"` 的 artifact，选择输出中的第一个，运行：
   ```bash
   openspec instructions <artifact-id> --change "<name>" --json
   ```
4. 解析 `context`、`rules`、`template`、`instruction`、`resolvedOutputPath` 和 `dependencies`。
5. 从磁盘重新读取所有已完成依赖，即使对话中已读过；用户可能已修改文件。
6. 以 `template` 为结构，遵循 `instruction`，将 `context` 与 `rules` 仅作为约束，不得复制进产物。
7. 将文件写入 `resolvedOutputPath`；若它是 glob，根据 schema 指令与变更上下文选择具体路径。
8. 验证文件存在后运行 `openspec status --change "<name>"`，展示进度和新解锁的 artifact，然后停止。本次只创建一个 artifact。

## 常见 spec-driven 产物

- `proposal.md`：Why、What Changes、Capabilities、Impact。
- `specs/<capability>/spec.md`：按 capability 创建，不使用变更名代替 capability 名。
- `design.md`：技术决策、架构和实现方法。
- `tasks.md`：带复选框的实施任务。

其他 schema 必须服从 CLI 的 `instruction`。

## 约束

- 每次只创建一个 artifact，不跳级、不乱序。
- 创建前重新读取依赖文件。
- 上下文不清楚时先询问。
- 不把 `<context>`、`<rules>`、`<project_context>` 写入 artifact。
- 所有 artifact ID 和顺序均以 schema 输出为准。
