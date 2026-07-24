# 目录用途

`tests/unit/gateway/` 存放 Workspace Gateway 控制面、路由与运行时基础设施的单元测试。

## 可修改内容

- Gateway 单元测试和测试专用 fixture。
- 按生产模块边界继续拆分的测试子目录。

## 不可修改内容

- 不在此放置需要启动真实 Gateway 或工作区后端的端到端测试。
- 不写入真实用户的 `${BOXTEAM_HOME:-~/.boxteams}/` 控制面数据。

## 规范

- 测试必须使用临时目录隔离 Gateway 状态。
- 测试目录结构应与 `app/gateway/` 的生产模块边界对应。
