# tests/unit/agents/tools/apply_patch

## 目录用途

存放 `app/agents/tools/apply_patch/` 补丁解析、匹配和条件发布的单元测试。

## 可修改内容

- 补丁解析、上下文匹配、文件移动和逐路径部分结果测试。
- 测试专用文件树与故障注入 fixture。

## 不可修改内容

- 不修改真实工作区文件。
- 不把其他 Agent 工具测试放入本目录。

## 规范

- 所有写入必须位于 pytest 临时目录。
- preflight 失败必须断言零写入；发布失败核实已发布、未发布、未尝试及未知路径，不要求或伪造整体回滚。
