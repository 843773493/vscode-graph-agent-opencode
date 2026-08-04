---
name: openspec-new-change
description: 使用分步 artifact 工作流启动新的 OpenSpec 变更。用户希望以结构化方式创建新功能、修复或修改，并希望使用中文时使用。
allowed-tools: Bash(openspec:*)
license: MIT
metadata:
  author: openspec
  version: "1.0-zh-cn"
---

# 新建 OpenSpec 变更

**输入：**应包含 kebab-case 变更名或待构建内容的描述。输入不明确时，询问用户想构建或修复什么，并从描述推导 kebab-case 名称；未理解目标前不得继续。

**Store 选择：**用户指定 store 时先用 `openspec store list --json` 解析 ID，并对支持的命令附加 `--store <id>`。

## 步骤

1. 确定 workflow schema。除非用户明确指定 schema，否则省略 `--schema` 使用默认值。用户询问可用工作流时运行 `openspec schemas --json` 并让其选择。
2. 运行 `openspec new change "<name>"`；仅在用户指定非默认 workflow 时添加 `--schema <name>`。
3. 运行 `openspec status --change "<name>" --json`，使用返回的 `planningHome`、`changeRoot`、`artifactPaths` 和 `nextSteps`，不要假设仓库内固定路径。
4. 找到第一个 `status: "ready"` 的 artifact，运行 `openspec instructions <artifact-id> --change "<name>"`，展示模板与上下文。
5. 停止并等待用户指示，不创建任何 artifact。

## 输出

说明变更名称和位置、schema 及 artifact 顺序、当前 `0/N` 状态、第一个 artifact 的模板，并询问用户是否准备创建。

## 约束

- 只创建变更脚手架，不创建 artifact。
- 名称不是 kebab-case 时要求修正。
- 同名变更已存在时建议继续现有变更。
- 非默认 workflow 必须传递 `--schema`。
