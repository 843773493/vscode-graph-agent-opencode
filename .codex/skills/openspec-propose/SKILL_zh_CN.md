---
name: openspec-propose
description: 一步提出新的 OpenSpec 变更并生成全部 artifact。用户希望快速描述目标并获得可实施的 proposal、spec、design 和 tasks，且希望使用中文时使用。
allowed-tools: Bash(openspec:*)
license: MIT
metadata:
  author: openspec
  version: "1.0-zh-cn"
---

# 提出 OpenSpec 变更

创建变更，并一次生成实施前需要的 proposal、spec、design 与 tasks。

**输入与 Store：**需要 kebab-case 名称或清晰描述；否则先询问。指定 store 时用 `openspec store list --json` 解析，并为支持的命令附加 `--store <id>`。

## 步骤

1. 运行 `openspec new change "<name>"` 创建含 `.openspec.yaml` 的变更脚手架。
2. 运行 `openspec status --change "<name>" --json`，读取 `applyRequires`、artifact 依赖、`planningHome`、`changeRoot`、`artifactPaths` 和 `actionContext`。
3. 使用任务列表跟踪 artifact 创建进度。
4. 按依赖顺序处理所有 `ready` artifact：
   - 运行 `openspec instructions <artifact-id> --change "<name>" --json`。
   - 重新读取依赖文件。
   - 使用 `template` 的结构和 `instruction` 的指导写入 `resolvedOutputPath`。
   - 将 `context`、`rules` 视为约束，不复制进文件。
5. 每次写入后验证文件并重新运行 status，直到 `applyRequires` 全部 `done`。
6. artifact 需要用户输入时先澄清；其余情况合理决策并继续。
7. 展示最终状态，汇总变更位置、已创建 artifact 和实施入口 `/opsx:apply`。

## 约束

- 创建实施所需全部 artifact。
- 从磁盘重新读取依赖，不依赖对话记忆。
- 同名变更存在时询问是继续还是另建。
- 不把 `<context>`、`<rules>`、`<project_context>` 写入 artifact。
- artifact ID、路径与顺序全部服从 CLI/schema。
