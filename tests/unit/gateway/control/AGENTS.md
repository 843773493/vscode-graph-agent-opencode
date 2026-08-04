# tests/unit/gateway/control

## 目录用途

存放 `app/gateway/control/` 控制面协调、调度和存储逻辑的单元测试。

## 可修改内容

- Generator 协调器、调度器和控制面 schema 的单元测试。
- 测试专用 fake 与 fixture。

## 不可修改内容

- 不启动真实 Gateway 或 Workspace 进程。
- 不访问真实用户的 Gateway 控制面数据。

## 规范

- 所有持久化状态必须使用 pytest 临时目录。
- 网络边界使用明确的 fake 或 mock，失败必须可观察。
