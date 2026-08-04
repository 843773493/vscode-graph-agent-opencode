---
name: openspec-update-change
description: 修订 OpenSpec 变更的既有规划 artifact 并保持相互一致。用户希望更新计划、纳入新决策或协调已有产物时使用；只改规划，绝不修改代码。
allowed-tools: Bash(openspec:*)
license: MIT
metadata:
  author: openspec
  version: "1.0-zh-cn"
---

# 更新 OpenSpec 变更

只修订既有规划 artifact 并保持一致，绝不修改实施代码。

**输入：**可选变更名。未提供时列出最近修改的 3–4 个变更，显示 schema、状态与修改时间；标记推荐项但必须由用户选择。指定 store 时解析并传递 `--store <id>`。

## 步骤

1. 运行 `openspec status --change "<name>" --json`，读取 schema、artifact 状态、路径和作用域。
2. artifact ID 与路径来自当前 schema，不得硬编码。只编辑 `artifactPaths.<id>.existingOutputPaths` 中已存在的具体文件；glob 的 `resolvedOutputPath` 不是可直接写入的文件。
3. 理解请求：明确修订时以该编辑为起点；仅说“更新/保持一致”时，对全部现有 artifact 做矛盾、缺口与重复审查。
4. 读取受影响 artifact 及其他既有 artifact。应用拟议修改后，双向检查所有产物的一致性；后期产物的修改也可能要求调整早期产物。
5. 只修改已存在文件。缺失 artifact 或 glob 下的新文件只记录，并建议 `/opsx:continue` 创建。
6. 逐个展示拟议修订及原因，得到用户确认后才写入。用户拒绝的修订保持原状。大幅重写前运行对应的 `openspec instructions <artifact-id> ... --json` 获取规则与模板。
7. 仅给出下一步建议，不代为执行：缺失 artifact 用 `/opsx:continue`；规划变化需落实代码用 `/opsx:apply`；全部完成用 `/opsx:archive`。

## 约束

- 只改规划，绝不改代码。
- 每项写入都需用户确认。
- 不推进 artifact 构建边界，不创建新产物或 glob 下新文件。
- 若请求改变变更意图而非细化，建议使用 `/opsx:new` 重新开始。
