# 目录用途

存放浏览器 Web UI 的真实端到端测试，覆盖页面初始化、交互和可量化性能行为。

## 可修改内容

- Playwright 浏览器测试驱动
- Web UI E2E 所需的隔离数据准备与指标断言
- 与本目录测试直接相关的 fixture 和辅助函数

## 不可修改内容

- 产品前端与后端实现
- `asset/` 中的只读测试模板
- 项目根目录或其他 E2E 的运行数据
- Playwright `route.fulfill()`、响应修改、模型 stub、替代 Gateway/后端或测试页面服务

## 规范

- 每个测试使用 `out/tests/e2e/clients/web/<测试文件名>/workspace/` 和 `artifacts/`
- 性能测试必须输出机器可读 JSON，并在断言失败时保留截图
- 指标必须同时覆盖响应时间、数据传输量和页面节点规模，不能只依赖固定等待时间
- 浏览器必须连接本测试启动的隔离 Gateway，不得复用开发环境端口
- 纯 Web 是当前唯一实现的客户端 E2E；不得据此宣称 Electron、VS Code 或 React Native 已通过
- 缺少 Chromium 或真实服务时明确报告未满足前置条件，不得切换到替身
