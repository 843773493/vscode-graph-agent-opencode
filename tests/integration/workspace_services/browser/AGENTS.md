# 目录用途

存放 Browser Manager 使用测试页面服务、受控网络条件或其他替身边界的集成测试。

## 可修改内容

- 浏览器资源、性能、帧治理和失败恢复集成场景。

## 不可修改内容

- 不要把测试 HTTP 页面服务描述为真实外部 E2E 依赖。

## 规范

- 工作区、端口和产物按测试文件隔离到 `out/tests/integration/workspace_services/browser/`。
