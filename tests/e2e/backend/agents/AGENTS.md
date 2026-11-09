# 目录用途

存放需要经过真实 Backend HTTP 的 Agent 后端 E2E。Debugging 工具的 Node Inspector 和 owner 隔离语义由 `tests/integration/backend/agents/test_debug_tools.py` 通过真实 Gateway Debug 插件验证；Workbench 的真实双 Thread／Agent 调试链另由客户端集成测试验收。

## 可修改内容

- 新增确实需要用户级后端 HTTP 路径的 Agent E2E。
- 调整本目录后续 E2E 的隔离工作区、请求和用户可见断言。

## 不可修改内容

- 不在本目录直连构造业务 service 来替代产品 Backend 入口。
- 不以插件 fixture 或 Backend mock 冒充真实 Gateway、Node 进程或浏览器流程。

## 规范

- 使用 `tests/e2e/conftest.py` 的 `client` 与 `e2e_workspace_root_path` 隔离运行。
- 需要调试插件时，经真实 Gateway 安装并调用 Debug 插件，不直接启动 Backend 内的旧调试实现。
- 正式产物写入对应的 `out/tests/e2e/backend/agents/` 镜像目录；禁止在项目根或 `asset/` 写测试输出。
