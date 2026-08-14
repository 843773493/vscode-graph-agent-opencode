# src/clients/electron

## 目录用途

TODO：预留未来 Electron 客户端的 main、preload 和 renderer 边界；当前没有实现。

## 可修改内容

- 仅可更新本目录的规划说明和 TODO。

## 不可修改内容

- 未经独立 OpenSpec 和用户明确要求，不得新增 Electron 产品代码、依赖或测试。
- 不得复制纯 Web 应用形成分叉。

## 规范

- 未来 renderer 应复用 `clients/shared/web-ui`，main/preload 只暴露最小安全桥接。
- 当前纯 Web 功能不需要同步到本目录。
