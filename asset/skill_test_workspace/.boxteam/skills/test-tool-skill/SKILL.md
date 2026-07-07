---
name: test-tool-skill
description: Use when the user mentions test_tool_skill, test-tool-skill, hidden skill tool testing, or asks to load a skill and return the exact text produced by this skill's validation tool.
allowed-tools: test_tool_2
metadata:
  prompt_alias: test_tool_skill
---

# Test Tool Skill

## 操作流程

1. 调用 `test_tool_2`。
2. 读取工具返回的文本。
3. 最终回复只输出工具返回的文本，不要添加解释、标点、代码块或额外空行。

## 成功标准

最终回复必须与 `test_tool_2` 返回值完全一致。
