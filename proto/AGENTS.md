# 目录用途

`proto/` 是跨进程公开协议的唯一源目录，保存 Gateway、Workspace、Terminal、Browser 与公共基础协议的 `.proto` 文件。

## 可修改内容

- 可以新增或修改版本化的 Protobuf 消息、枚举、`oneof` 和跨文件 import。
- 可以调整协议域的文件组织，但必须保持 package 与版本边界清晰。

## 不可修改内容

- 不要把 Workspace 内部事件总线、Agent 内部状态或持久化模型直接放入这里。
- 不要手工修改生成目录中的 Python、JavaScript 或 TypeScript 文件。

## 规范

- 使用 `boxteam.<domain>.v<version>` package 命名。
- 已发布字段编号不得复用；不再使用的字段必须保留并标记为 reserved。
- 协议变更必须通过 Buf 格式、依赖和 breaking 检查。
