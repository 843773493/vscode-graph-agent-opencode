## 1. 协议工具链与基线

- [x] 1.1 记录当前 HTTP JSON、SSE、Terminal WebSocket 和 Browser WebSocket 的代表性请求、响应、错误和事件快照，作为迁移兼容基线
- [x] 1.2 添加 `proto/`、Buf workspace、生成配置和协议生成命令，并明确 Python、Node/JavaScript、Web TypeScript 的输出目录
- [x] 1.3 为新增协议源码、生成绑定、codec 和 adapter 目录补齐符合仓库规范的 `AGENTS.md`

## 2. 协议域与核心绑定

- [x] 2.1 建立 `common/v1`、`gateway/v1`、`workspace/v2`、`terminal/v1` 和 `browser/v1` 的 package 与版本骨架
- [x] 2.2 迁移少量公共基础、Message、Job、Workspace 会话事件、Terminal 消息和 Browser 消息样例，验证跨文件 import 和 `oneof` 生成结果
- [x] 2.3 生成并检查 Python、Node/JavaScript 和 Web TypeScript 绑定，确保输出目录与协议源目录对应且删除消息不会残留生成物
- [x] 2.4 增加协议格式、依赖、package 版本和 breaking change 检查

## 3. Python 边界适配

- [x] 3.1 实现 Protobuf 与现有 JSON 字段、optional、Timestamp、Struct 和错误对象之间的 Python codec
- [x] 3.2 实现 Workspace 内部事件到 Workspace SSE 协议事件的映射，并对未支持事件返回详细错误
- [x] 3.3 将 Terminal Manager Client 和 Browser Manager Client 的传输编码迁移到对应协议 adapter，保持业务抽象接口不变
- [x] 3.4 为 Gateway 控制面增加只依赖 `common` 和 `gateway` 协议的边界 adapter，禁止引入 Terminal、Browser 或 Workspace 业务协议

## 4. Terminal 与 Browser 服务迁移

- [x] 4.1 将 Terminal Manager 的 HTTP/WS 请求、输出、状态、attach、resize 和完成消息接入 `terminal/v1` codec
- [x] 4.2 将 Browser Manager 的页面、输入、帧、下载和状态消息接入 `browser/v1` codec
- [x] 4.3 让 Terminal Frontend 和 Browser Frontend 使用各自服务协议的客户端绑定；本地 DOM、快捷键和展示状态继续留在前端
- [x] 4.4 为两个辅助服务增加协议错误、连接断开、消息顺序和资源标识的契约测试

## 5. Workspace 与 Gateway 接入

- [x] 5.1 将 Workspace HTTP/SSE 边界接入 Workspace 协议 codec，保持现有 API 路径、JSON 字段、错误传播和 SSE 事件顺序
- [x] 5.2 将 Gateway 的控制面和透明代理边界接入 Gateway 协议；代理 Workspace/Terminal/Browser 数据时不复制业务 schema
- [x] 5.3 从协议 descriptor 和路由元数据生成或补充 OpenAPI/SSE runtime schema，并覆盖代表性消息
- [x] 5.4 增加跨进程契约测试，覆盖 Workspace↔Gateway、Workspace↔Terminal Manager、Workspace↔Browser Manager 和 Web↔SSE

## 6. Web 类型与运行时校验迁移

- [x] 6.1 将 Web `types/backend.ts`、SSE 类型和协议客户端迁移到新的生成绑定与 adapter 入口
- [x] 6.2 将 SSE runtime validator 的输入 schema 改为协议生成结果，确保动态 metadata/raw payload 不被静默丢弃
- [x] 6.3 运行 Web 静态分析和 `bun run --cwd src/clients/web build`，修复生成类型、ESM import 和协议适配问题

## 7. 后续全量迁移门槛

> 本阶段按设计只迁移协议框架和少量代表性业务域。以下任务必须在其余公开业务 DTO 完成 `.proto` 定义和消费者迁移后执行，不能通过删除仍被使用的类型生成物来“完成”。

- [x] 7.1 按协议域确认所有 API、服务和 Web 消费者已经迁移，并删除不再使用的公共 Pydantic DTO 分支
- [x] 7.2 删除 `pydantic2ts` 公共类型生成链路、重复生成文件和失效的显式导出
- [x] 7.3 保留必要的内部 Pydantic 模型，但禁止其继续作为公开 TypeScript 协议来源
- [x] 7.4 运行 Buf 检查、Python 测试、Node/JavaScript 服务测试、Web 构建和 HTTP/SSE/WS 集成测试，确认首阶段协议适配可发布
