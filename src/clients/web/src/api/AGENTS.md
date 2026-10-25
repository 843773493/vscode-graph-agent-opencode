# 目录用途

存放浏览器前端按业务域拆分的 API 客户端，以及所有客户端共享的 HTTP 传输基础设施。业务端点在所属领域模块中定义；共享 SSE 帧原语位于 `../sse/`，跨订阅连接的编排位于 `stream/workspaceObservation.ts`。

## 职责树

```text
api/
├── http.ts                         Gateway 身份屏障、HTTP 请求与 JSON 解包
├── workspace.ts                    工作区信息与默认模型设置
├── workspaceIdentity.ts            Gateway 工作区目标和身份头
├── workspaceFilesystem.ts          工作区文件查询、变更、上传和下载
├── workspaceResources.ts           工作区资源请求
├── jobs.ts                         Agent 作业请求
├── nodeDebug.ts                    Node 调试请求
├── toolTesting.ts                  工具测试请求
├── gatewayTokenCache.test.ts       Gateway token 缓存契约
├── http.test.ts                    HTTP 请求和响应契约
├── requestBarrier.test.ts           Gateway 屏障契约
├── workspace*.test.ts               工作区 API 契约测试
├── nodeDebug.test.ts                Node 调试 API 契约测试
├── gateway/
│   ├── sessionConnections.ts       Gateway 会话连接请求
│   ├── userAccess.ts                Gateway 用户访问请求
│   └── userViewState.ts             Gateway 用户视图状态请求
├── session/
│   ├── sessions.ts                  会话列表、详情、线程和 fork 请求
│   ├── sessionCatalog*.ts           会话目录查询与变更请求
│   ├── sessionActivity.ts            会话活动历史与 SSE endpoint
│   ├── sessionMessages.ts            消息、附件与执行请求
│   ├── sessionApiFetchMock.ts          会话 API 单测共用 fetch 桩
│   ├── sessionTurnHistory.ts         Turn 历史与分页请求
│   ├── sessionContext.ts             会话上下文读取
│   ├── sessionGoals.ts               会话目标读写
│   ├── sessionResources.ts           会话资源读取和控制
│   ├── sessionSkillTracking.ts       会话技能跟踪请求
│   └── *.test.ts                     各会话 API 的就近契约测试
├── stream/
│   ├── workspaceObservation.ts       多订阅者共享 Gateway SSE 观察连接
│   ├── sessionMessageStream.ts       Turn 消息事件流
│   ├── sessionTraceStream.ts         Trace 历史和事件流
│   ├── workspaceFileEvents.ts        工作区文件事件流
│   ├── messageStreamSnapshot.ts       消息快照严格校验
│   └── *.test.ts                     各事件流 API 的就近契约测试
└── tests/
    ├── AGENTS.md                     跨 API 测试职责边界
    ├── api.goal.test.ts              会话目标 API 导出契约
    ├── gatewayApi.test.ts             Gateway API 契约
    ├── toolTesting.test.ts            工具测试 API 契约
    └── workspaceFileEventStreamApi.test.ts 文件事件流 API 契约
```

# 可修改内容

- 按所属业务域维护请求构造、响应解包与专用 HTTP 错误。
- 维护共享 HTTP 屏障，以及 stream 层复用的观察连接编排。

# 不可修改内容

- 不维护 React 展示状态或会话业务状态。
- 不在客户端推导后端协议中不存在的默认业务数据。
- 不把 SSE 帧解析复制到 API 端点；使用 `../sse/` 的共享实现。

# 规范

- 业务 API 按语义模块组织，根级 `api.ts` 只重导出迁移后的公开符号。
- 专用 HTTP 错误定义在其所属业务客户端中。
- 请求失败必须透明抛出，不得静默降级。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。

模板示例；在整理 `AGENTS.md` 时请保留此行。
