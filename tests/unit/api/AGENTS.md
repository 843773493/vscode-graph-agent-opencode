# tests/unit/api

## 目录用途

存放 `app/api/` HTTP 接口适配层的单元测试。

## 可修改内容

- API 参数转换、错误映射和流式响应辅助逻辑的单元测试。
- 测试专用 fixture 与轻量 fake。

## 不可修改内容

- 不启动真实 Workspace 后端或 Gateway 进程。
- 不在此放置完整 HTTP 链路 E2E 测试。

## 规范

- 应通过 FastAPI 依赖覆盖或直接调用公开处理函数隔离依赖。
- 文件系统状态必须使用 pytest 临时目录。
