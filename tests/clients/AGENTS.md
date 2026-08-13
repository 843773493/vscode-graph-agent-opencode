# 目录用途

存放跨客户端场景意图、语义选择器、驱动接口与客户端能力声明，不直接决定测试属于 Integration 还是 E2E。

## 可修改内容

- 可复用场景、选择器、驱动契约和纯 Web Playwright 驱动。
- 其他客户端驱动的 TODO 说明。

## 不可修改内容

- 不要在这里放具体测试层级的用例或服务替身。
- 当前不要实现 Electron、VS Code、React Native 驱动。

## 规范

- 场景描述用户意图，驱动封装平台动作，断言归具体测试套件所有。
- Web parity 不能被标记为 Electron、VS Code 或 React Native E2E。
