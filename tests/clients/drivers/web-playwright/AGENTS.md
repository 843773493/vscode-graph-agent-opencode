# 目录用途

实现当前纯 Web 客户端的 Playwright 动作、等待和可审计产物收集。

## 可修改内容

- 浏览器启动、页面动作、只读网络/控制台观测和 trace。

## 不可修改内容

- E2E 驱动不得 fulfill 或修改产品依赖响应；需要路由替身的场景必须归入 Integration。

## 规范

- 使用 Node Playwright ESM；截图和 trace 目录由运行上下文提供。
