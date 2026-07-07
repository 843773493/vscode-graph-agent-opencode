---
name: test-tool-2
description: 当用户要求调用 test_tool_2、验证 test_tool_2 返回值，或要求执行 test_tool_2 扩展工具时，读取本 skill。
allowed-tools: test_tool_2
---

# test_tool_2 扩展工具

## 适用场景

当用户要求调用 `test_tool_2`，或要求验证 `test_tool_2` 的返回值时，必须使用本 skill。

## 调用方式

`test_tool_2` 不会直接出现在模型 tools 列表中。必须发起真实工具调用，使用固定入口 `invoke_custom_tool`，参数如下：

```json
{
  "tool_name": "test_tool_2",
  "arguments": {}
}
```

## 输出要求

`test_tool_2` 不需要输入参数。工具返回后，最终回复只能包含工具返回文本本身。

禁止把 `invoke_custom_tool` 或上面的 JSON 当成普通正文输出；它们只能作为工具调用名称和参数使用。
