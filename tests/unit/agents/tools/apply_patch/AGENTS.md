# tests/unit/agents/tools/apply_patch

## 目录用途

存放 `app/agents/tools/apply_patch/` 补丁解析、匹配和事务执行的单元测试。

## 可修改内容

- 补丁解析、上下文匹配、文件移动和事务回滚测试。
- 测试专用文件树与故障注入 fixture。

## 不可修改内容

- 不修改真实工作区文件。
- 不把其他 Agent 工具测试放入本目录。

## 规范

- 所有写入必须位于 pytest 临时目录。
- 失败场景必须断言文件系统保持一致或完成明确回滚。
