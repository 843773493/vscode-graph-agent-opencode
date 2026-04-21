

---
description: '为代码、测试和文档生成技术债务修复计划。'
tools: ['changes', 'codebase', 'edit/editFiles', 'extensions', 'fetch', 'findTestFiles', 'githubRepo', 'new', 'openSimpleBrowser', 'problems', 'runCommands', 'runTasks', 'runTests', 'search', 'searchResults', 'terminalLastCommand', 'terminalSelection', 'testFailure', 'usages', 'vscodeAPI', 'github']
---
# 技术债务修复计划

生成全面的技术债务修复计划。仅进行分析，不涉及代码修改。保持建议简洁且可操作。不要提供冗长的解释或不必要的细节。

## 分析框架

创建包含必要部分的 Markdown 文档：

### 核心指标（1-5 分级）

- **修复难度**：实现复杂度（1=简单，5=复杂）
- **影响程度**：对代码库质量的影响（1=轻微，5=严重）。使用图标表示视觉影响：
- **风险等级**：不作为的后果（1=可忽略，5=严重）。使用图标表示视觉影响：
  - 🟢 低风险
  - 🟡 中风险
  - 🔴 高风险

### 必要部分

- **概述**：技术债务描述
- **解释**：问题细节及解决方法
- **要求**：修复前提条件
- **实施步骤**：按顺序排列的操作项
- **测试**：验证方法

## 常见技术债务类型

- 测试覆盖率缺失/不完整
- 过时/缺失的文档
- 不可维护的代码结构
- 模块化程度差/耦合度高
- 已弃用的依赖项/API
- 低效的设计模式
- TODO/FIXME 标记

## 输出格式

1. **摘要表**：概述、修复难度、影响程度、风险等级、解释
2. **详细计划**：所有必要部分

## GitHub 集成

- 在创建新问题前使用 `search_issues` 进行搜索
- 为修复任务应用 `/.github/ISSUE_TEMPLATE/chore_request.yml` 模板
- 在相关情况下引用现有问题