## Why

当前公开 DTO 由 Pydantic 模型逐文件转换为 TypeScript，模型依赖会在多个生成文件中重复展开，跨文件 import 和协议域边界无法由生成器可靠维护。项目同时包含 Workspace Backend、Gateway、Terminal Manager 和 Browser Manager 多个独立进程，需要一套能够明确表达跨进程依赖、版本和目录归属的协议源。

## What Changes

- 将跨进程公开协议迁移到以 `.proto` 为唯一来源的协议目录。
- 使用 Buf 管理协议模块、import、格式检查和 breaking change 检查。
- 为 Gateway、Workspace、Terminal、Browser 及公共基础类型建立独立的 package/version 边界。
- 生成 Python、Node/JavaScript 和 Web TypeScript 的协议绑定，保持生成代码与源协议的目录对应关系。
- 增加 HTTP JSON、SSE、Terminal WebSocket 和 Browser WebSocket 的协议适配层，首阶段保持现有外部 JSON 形状兼容。
- 保留 Workspace 内部事件总线和业务模型的独立性，仅在进程边界转换为生成的协议类型。
- 为已迁移的跨进程协议域停止新增 `pydantic2ts` 类型来源；所有业务 DTO 完成协议迁移后，再删除旧的逐模块生成链路和重复公共 DTO 生成物。**BREAKING**

## Capabilities

### New Capabilities

- `cross-process-protocol-contracts`: 为公共基础、Gateway、Workspace、Terminal 和 Browser 进程建立可版本化、可生成、可跨语言使用的 Protobuf 协议契约。

### Modified Capabilities

- 无。当前变更主要替换协议定义和代码生成机制；现有 Gateway、Workspace、Terminal、Browser 的业务行为在首阶段保持不变。

## Impact

- 新增 `proto/` 协议源、Buf 配置和多语言代码生成流程。
- 影响 `app/` 的 Workspace/Gateway 边界适配器、`src/workspace-services/` 的 Terminal/Browser 服务、`src/clients/web/` 的公共类型与 SSE 校验。
- 影响 HTTP/SSE/WS 的序列化、OpenAPI 组件和运行时 schema 生成。
- 新增 Protobuf/Buf 生成依赖及协议契约测试；现有 Pydantic 模型在迁移期间需要与协议适配层共存。
