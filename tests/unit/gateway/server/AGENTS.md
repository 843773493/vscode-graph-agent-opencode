# tests/unit/gateway/server

## 目录用途

存放 `app/gateway/server/` 启动装配、静态页面和工作区代理的单元测试。

## 可修改内容

- Server bootstrap、静态资源挂载和代理辅助逻辑测试。
- 测试专用 ASGI 应用与 HTTP transport。

## 不可修改内容

- 不启动长期运行的真实 Gateway 进程。
- 不将 Gateway 根模块或 runtime 测试混入本目录。

## 规范

- HTTP 测试必须使用进程内 transport 或 TestClient。
- 代理测试需明确断言流、响应头和错误传播行为。
