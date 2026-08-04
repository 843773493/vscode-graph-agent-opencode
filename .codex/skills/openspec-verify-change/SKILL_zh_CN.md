---
name: openspec-verify-change
description: 验证实施是否符合 OpenSpec 变更 artifact。用户希望在归档前检查完整性、正确性与一致性，并希望使用中文报告时使用。
allowed-tools: Bash(openspec:*)
license: MIT
metadata:
  author: openspec
  version: "1.0-zh-cn"
---

# 验证 OpenSpec 变更

**输入：**可选变更名。未提供时列出含 tasks artifact 的变更，显示 schema，将未完成项标记为“进行中”，并让用户选择。不得自动选择。指定 store 时解析并传递 `--store <id>`。

## 步骤

1. 运行 `openspec status --change "<name>" --json`，读取 schema、路径和已有 artifact。
2. 运行 `openspec instructions apply --change "<name>" --json`，读取 `contextFiles` 中所有可用文件。
3. 按三个维度组织报告，每项问题分为 CRITICAL、WARNING、SUGGESTION：
   - **完整性**：任务和 requirement 覆盖。
   - **正确性**：requirement 实施与 scenario 覆盖。
   - **一致性**：design 遵循度与项目模式一致性。
4. 完整性检查：
   - 统计 tasks 中 `- [ ]` 与 `- [x]`；每个未完成任务记为 CRITICAL，并给出可执行建议。
   - 从 delta spec 提取 `### Requirement:`，在代码库搜索实施证据；明显未实施时记为 CRITICAL。
5. 正确性检查：
   - 为每个 requirement 记录实现文件和行号，评估是否符合意图；偏离时记为 WARNING。
   - 检查每个 `#### Scenario:` 是否有代码处理和测试覆盖；缺失时记为 WARNING。
6. 一致性检查：
   - 从 design 中提取 Decision、Approach、Architecture 等关键决定，对照实现；冲突记为 WARNING。
   - 检查命名、目录、代码风格等项目模式；显著偏离记为 SUGGESTION。
7. 输出汇总表、按优先级分组的问题和最终判断。

## 判断原则

- 完整性聚焦客观清单；正确性允许基于搜索和文件分析做合理推断；一致性只报告明显问题。
- 不确定时降低严重级别：优先 SUGGESTION，其次 WARNING，最后才是 CRITICAL。
- 每个问题必须带具体建议，并在适用时给出 `file.ts:123`。
- artifact 不齐时降级检查：只有 tasks 就只验任务；tasks+specs 就跳过 design；必须说明跳过项及原因。
- 有 CRITICAL 时说明归档前必须修复；只有 WARNING 时说明可归档但应考虑改进；全通过时明确可归档。
