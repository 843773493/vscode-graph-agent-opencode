# src/clients/mobile

## 目录用途

TODO：预留未来 React Native 移动客户端的 core、native 和 web 运行面边界；当前没有实现。

## 可修改内容

- 仅可更新本目录的规划说明和 TODO。

## 不可修改内容

- 未经独立 OpenSpec 和用户明确要求，不得新增 React Native 产品代码、依赖或测试。
- 不得把 React DOM 组件当作原生组件复用。

## 规范

- 未来移动端只复用运行时无关的 `clients/shared/core` 和协议。
- RN Web 只能作为 parity/integration 运行面，不能代替原生 E2E。
