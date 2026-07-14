---
name: large-test-output
description: 当用户要求调用 large_test_output 或验证大工具输出落盘时，读取本 skill。
allowed-tools: large_test_output
---

# 大工具输出测试

必须通过固定入口 `invoke_custom_tool` 发起真实调用：

```json
{
  "tool_name": "large_test_output",
  "arguments": {}
}
```

工具不需要参数。工具结果中的完整文件包含一行：

```text
retrieval-target=<需要回答的值>
```

这个目标行位于头尾预览之外。收到大结果引用后必须完成以下步骤：

1. 使用 `grep`，在工具结果给出的 `read_path` 中搜索字面文本 `retrieval-target`，并使用包含匹配内容的输出模式。
2. 根据 grep 返回的行号，使用 `read_file` 和同一个 `read_path` 分段读取目标附近内容。
3. 最终只回复 `retrieval-target=` 后面的值。

禁止从 Skill 猜测目标值，也不能省略 grep 或 read_file。
