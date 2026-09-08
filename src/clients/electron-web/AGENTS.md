# src/clients/electron-web

## 目录用途

预留 Electron renderer 的浏览器可运行 parity 客户端。它承载桌面布局的非原生页面，使约 90% 的 Electron renderer 功能可以在普通浏览器中开发和测试。

## 可修改内容

- 桌面布局、React DOM 组件、页面状态和 Gateway API 交互。
- 对 Electron bridge 的浏览器 mock 或无原生能力测试适配。

## 不可修改内容

- 不得放 Electron main/preload 实现或直接导入 Node.js、Electron API。
- 不得复制 `web/` 形成第二套桌面业务逻辑。
- 不得把浏览器 parity 测试结果当作真实 Electron 原生验证。

## 规范

- 与 `web/` 共享 `shared/web-ui` 和 `shared/core`；运行入口、资源加载和 bridge 适配保持独立。
- 所有产品业务状态仍通过 Gateway API 和共享状态模型取得，不能由本地 React 状态伪造成功。
