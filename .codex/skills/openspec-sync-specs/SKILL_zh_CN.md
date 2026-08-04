---
name: openspec-sync-specs
description: 将 OpenSpec 变更中的 delta spec 同步到主 spec。用户希望更新主规格但暂不归档变更，并希望使用中文工作流时使用。
allowed-tools: Bash(openspec:*)
license: MIT
metadata:
  author: openspec
  version: "1.0-zh-cn"
---

# 同步 OpenSpec 规格

这是由 Agent 驱动的智能合并：读取 delta spec，并直接编辑主 spec；局部场景更新不应替换整个 requirement。

**输入：**可选变更名。未提供时运行 `openspec list --json`，只展示含 delta spec 的变更并让用户选择，不得猜测。指定 store 时解析 ID 并传递 `--store <id>`。

## 步骤

1. 运行 `openspec status --change "<name>" --json`。主 spec 根目录必须使用 `<planningHome.root>/openspec/specs/`，不得硬编码当前仓库路径。
2. 从 `artifactPaths.specs.existingOutputPaths` 获取 delta spec 列表；没有文件时告知用户并停止。
3. 对每个 capability：
   - 读取 delta spec。
   - 读取 `<planningHome.root>/openspec/specs/<capability>/spec.md`；文件可能尚不存在。
   - `ADDED Requirements`：不存在则新增；已存在则按隐式 MODIFIED 更新。
   - `MODIFIED Requirements`：只应用描述或场景变更，保留 delta 未提及的内容。
   - `REMOVED Requirements`：删除完整 requirement 块。
   - `RENAMED Requirements`：把 FROM requirement 重命名为 TO。
4. capability 尚不存在时创建主 spec，加入简短的 Purpose（可标记 TBD）及 ADDED requirements。
5. 汇总更新的 capability 及新增、修改、删除、重命名数量。

## Delta spec 格式

识别 `## ADDED Requirements`、`## MODIFIED Requirements`、`## REMOVED Requirements`、`## RENAMED Requirements`；requirement 使用 `### Requirement:`，场景使用 `#### Scenario:`，重命名使用 `FROM:`/`TO:`。

## 约束

- 修改前同时读取 delta 与主 spec。
- delta 表达意图，不是整文件替换。
- 保留 delta 未提及的主 spec 内容。
- 不清楚时询问用户。
- 操作必须幂等，重复运行不产生额外变化。
