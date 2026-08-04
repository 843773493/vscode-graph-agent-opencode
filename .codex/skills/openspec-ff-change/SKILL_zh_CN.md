---
name: openspec-ff-change
description: 快进创建 OpenSpec artifact。用户希望一次生成实施所需全部规划产物、不逐个确认，并希望使用中文时使用。
allowed-tools: Bash(openspec:*)
license: MIT
metadata:
  author: openspec
  version: "1.0-zh-cn"
---

# 快进创建 OpenSpec 变更产物

一次生成进入实施阶段所需的全部 artifact。

**输入：**kebab-case 变更名或待构建内容描述。目标不明确时先询问，不得继续。指定 store 时解析 ID，并为支持的命令添加 `--store <id>`。

## 步骤

1. 运行 `openspec new change "<name>"` 创建变更。
2. 运行 `openspec status --change "<name>" --json`，读取 `applyRequires`、artifact 依赖、路径和作用域上下文。
3. 使用任务列表跟踪 artifact 创建进度。
4. 按依赖顺序循环处理 `ready` artifact：
   - 运行 `openspec instructions <artifact-id> --change "<name>" --json`。
   - 解析 `context`、`rules`、`template`、`instruction`、`resolvedOutputPath`、`dependencies`。
   - 从磁盘重新读取所有依赖文件。
   - 以模板为结构创建文件；`context` 与 `rules` 只作约束，不复制到文件。
   - 简短报告“✓ 已创建 <artifact-id>”。
5. 每创建一个 artifact 后重新运行 status。直到 `applyRequires` 中所有 ID 均为 `done`。
6. 若关键信息不清楚，询问用户后继续；能合理决定时保持推进。
7. 最后运行 `openspec status --change "<name>"`，汇总位置、产物及“已准备实施”。

## 约束

- 创建 schema 的 `apply.requires` 定义的全部产物。
- 不硬编码 artifact 类型或路径。
- 每个文件写入后验证存在。
- 同名变更已存在时建议继续该变更。
- 绝不把上下文约束块写入产物正文。
