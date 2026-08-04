# tests/unit/runtime

## 目录用途

存放 `app/runtime/` Agent 运行时、认证运行时和会话编排入口的单元测试。

## 可修改内容

- 运行时构建、会话请求编排和认证状态解析测试。
- 测试专用 fake、mock 和 fixture。

## 不可修改内容

- 不请求真实模型或外部认证服务。
- 不启动完整 Workspace 后端进程。

## 规范

- 外部边界必须显式替换，异常不得被静默吞掉。
- 异步测试应使用 pytest-asyncio 并确保资源完成清理。
