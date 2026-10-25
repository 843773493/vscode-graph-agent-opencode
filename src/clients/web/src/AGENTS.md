# 目录用途

`src/clients/web/src/` 是浏览器桌面工作台的源码根目录，负责页面组合、界面交互、客户端状态和浏览器内数据处理。它不承载 Electron 原生宿主、React Native 或 Python 后端实现。

## 职责树

```text
src/
├── main.tsx, App.tsx           应用入口与主窗口组合
├── hooks.tsx                   AppProvider、全局状态与 hook 装配
├── index.css                   全局基础样式
├── shared.d.ts                 全局声明与跨模块环境类型
├── api.ts                      工作区、会话等 API 的公开导出
├── gatewayApi.ts                Gateway 控制面 API
├── pendingRequestsApi.ts        待处理请求 API
├── api/                         HTTP 传输与按领域拆分的请求客户端
│   ├── http.ts                  统一鉴权、重试、超时、JSON 请求与响应解包
│   ├── workspace*.ts            工作区、文件、资源与身份请求
│   ├── jobs.ts, nodeDebug.ts     作业与 Node 调试请求
│   ├── toolTesting.ts            工具测试请求
│   ├── gateway/                  Gateway 会话连接、用户访问与视图状态
│   ├── session/                  会话、目录、消息、目标、资源及活动订阅
│   ├── stream/                   消息、Trace、文件事件与共享观察流编排
│   └── tests/                    跨 API 模块的契约测试与 AGENTS.md 边界说明
├── hooks/                        按会话、工作区和界面领域组织的副作用流程
├── state/                        状态归并、时间线、展示投影与本地状态存储
├── components/                   按聊天、工作区、面板和窗口区域组织的 React UI
├── layout/                       工作台尺寸和布局纯计算
├── theme/                        运行时主题应用
├── styles/                       按基础样式和界面领域组织的 CSS
├── protocol/                     协议载荷解析、校验与 JSON/SSE 转换
├── sse/                          SSE 帧消费、载荷校验与共享空闲期限
├── runtime/                      重量级依赖的按需加载入口
├── types/                        前端类型和后端协议类型
│   ├── backend.ts, frontend.ts    手写后端 DTO 与前端视图类型
│   ├── gatewayProtocol.ts         Gateway 协议类型
│   ├── protocol.ts                协议类型汇总入口
│   ├── toolTesting.ts              工具测试接口类型
│   ├── openapi/                   生成的 OpenAPI schema
│   ├── protocol_buf_generated/    生成的 Protobuf TypeScript 绑定
│   └── protocol_generated/        生成的协议类型与运行时校验 schema
├── utils/                         可复用浏览器工具
│   ├── markdown/                  Markdown 渲染与链接目标解析
│   ├── media/                     媒体附件构造与归一
│   └── selection/                 浏览器元素和工作区目录选择
└── tests/                         跨源码目录的契约测试与共享测试装具
    ├── testGlobals.ts             全局测试环境清理与最小浏览器桩
    ├── workspaceFileIntentStoreMock.ts 工作区文件意图状态测试桩
    ├── webSelectors.contract.test.ts 跨客户端 DOM 选择器契约
    └── fixtures/                  多个源码目录共用的测试数据与装具
```

按职责选层：请求与响应放 `api/`；React 副作用、竞态和订阅放 `hooks/`；纯状态变换放 `state/`；跨领域且不拥有状态的通用函数放 `utils/`；协议线格式转换放 `protocol/` 或 `sse/`；React 展示放 `components/`。功能专属测试靠近实现，只有多个源码领域共同使用的测试装具才放 `tests/fixtures/`。

# 可修改内容

- 浏览器桌面工作台的组件、状态、API 客户端、样式和类型。
- 依据现有职责边界细分源码模块，并同步维护对应目录的 `AGENTS.md` 树形说明。

# 不可修改内容

- 不引入 Node.js 专用模块作为浏览器运行时依赖。
- 不在本目录实现 VS Code 扩展宿主、Electron main/preload、React Native 或后端业务逻辑。
- 不让前端成为业务状态权威来源；服务端业务状态变更必须经过 API。

# 规范

- 页面功能继续在 `src/clients/web` 实现；其他客户端按各自运行面维护。
- 组件负责渲染和局部交互；API、状态变换和副作用遵循上方职责分层。
- 重量级依赖放入 `runtime/` 并按需加载；纯客户端核心不得依赖浏览器全局对象。
- 每个新增源码子目录都要包含“目录用途、可修改内容、不可修改内容、规范”四部分的 `AGENTS.md`。
- 用户可见文案和代码注释使用中文，专业术语除外。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。

模板示例；在整理 `AGENTS.md` 时请保留此行。
