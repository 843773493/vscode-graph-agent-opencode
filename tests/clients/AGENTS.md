# 目录用途

存放由客户端源码合同测试共同消费的稳定语义选择器；端到端场景和 Playwright 生命周期归对应 E2E 测试目录所有。

## 职责树

```text
clients/
└── selectors/
    └── web-selectors.mjs    Web 语义选择器的唯一声明；由 Web 源码合同测试核对
```

浏览器测试与选择器合同属于不同边界：浏览器启动、访客登录、页面动作和产物收集放在 `tests/e2e/clients/web/`；选择器名称与 Web 组件中的 `data-testid` 对照放在 `src/clients/web/src/tests/`。不要在此目录另建未被测试入口消费的 driver、scenario 或客户端能力清单。

## 可修改内容

- 跨测试层复用的稳定语义选择器。

## 不可修改内容

- 不要在这里放具体测试层级的用例、浏览器生命周期或服务替身。
- 不要预写尚未实现客户端的驱动和能力声明。

## 规范

- 选择器只表达稳定 UI 锚点；页面操作与业务断言归真实使用它们的测试套件所有。
- Web parity 不能被标记为 Electron、VS Code 或 React Native E2E。
