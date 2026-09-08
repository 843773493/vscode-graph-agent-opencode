# src/clients/mobile

## 目录用途

预留未来 React Native 移动客户端的原生 UI 和宿主能力边界；移动 Web parity 客户端位于同级 `mobile-web/`，不放入本目录。

## 可修改内容

- React Native 屏幕、原生组件和移动端宿主适配规划。

## 不可修改内容

- 未经独立 OpenSpec 和用户明确要求，不得新增 React Native 产品代码、依赖或测试。
- 不得把 React DOM 组件或 `mobile-web` 测试实现当作原生组件复用。

## 规范

- 移动端只复用运行时无关的 `clients/shared/core` 和协议。
- `mobile-web` 只能作为非原生 parity/integration 运行面，不能代替模拟器或真机 E2E。
