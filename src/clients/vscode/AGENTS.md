# src/clients/vscode

## 目录用途

TODO：预留未来统一迁入的 VS Code extension 与 webview 客户端边界；现存实现暂留在 `src/extension.js`、`src/backend/`、`src/webview/` 和 `src/webview-ui/`。

## 可修改内容

- 仅可更新本目录的迁移规划说明和 TODO。

## 不可修改内容

- 未经独立 OpenSpec 和用户明确要求，不得迁移、复制或重构现存 VS Code 实现。
- 当前纯 Web 功能不需要同步到旧 Webview UI。

## 规范

- 后续迁移必须一次更新扩展清单、资源路径、构建和 E2E，不保留双路径实现。
- 本目录当前不得包含产品代码。
