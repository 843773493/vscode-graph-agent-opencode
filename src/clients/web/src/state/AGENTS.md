# 目录用途

`src/clients/web/src/state/` 放浏览器前端的状态派生、聚合和纯数据转换逻辑，服务于 `../hooks.tsx` 中的 AppProvider。

## 职责树

以下是逻辑索引；各文件仍保留在仓库当前路径，分组说明状态数据从哪里来、由哪一层整理：

```text
state/
├── 会话与对话
│   ├── session/                 会话标识、目录投影、turn 历史与会话诊断
│   ├── conversations.ts         对话列表的状态入口
│   ├── conversations/           消息流、pending 请求和 turn 到对话的投影
│   ├── composerState.ts         当前编辑器草稿状态
│   ├── composerDrafts/          按会话保存的草稿存储
│   ├── attachments.ts           消息附件展示数据
│   ├── childThreadDisplay.ts    子 Thread 展示投影
│   │   └── childThreadDisplay.test.ts
│   ├── creatableSessionConnectionOptions.ts 创建终端/浏览器等会话连接的菜单选项
│   └── sessionGoal.ts           会话目标与相关命令状态
├── 实时输出与诊断展示
│   ├── messageStream/           实时消息事件 reducer、快照水合和响应投影
│   ├── timeline/                消息与响应内容的时间线投影
│   ├── trace/                   Trace 聚合成工具和活动条目
│   ├── traceEvents.ts           Trace 事件队列、去重和终态归并
│   ├── tracePayload.ts          Trace 载荷规范化与详情提取
│   ├── display/                 Agent、工具、资源与事件展示文本
│   └── requestLogDisplay/       模型请求日志的消息、工具和重放视图
├── 工作区与 Gateway
│   ├── gatewayWorkspaceState.ts Gateway 工作区列表投影
│   ├── workspaceInformation.ts  工作区诊断信息
│   ├── workspaceBottomPanel.ts  工作区底部面板展示偏好
│   ├── workspaceFileTreeEvents.ts 文件树变更事件
│   └── workspaceFiles/          待确认操作和上传 Blob 的 IndexedDB 存储
├── 输入、资源与偏好
│   ├── contextInspection/       上下文资源分页
│   ├── customTools/             自定义工具协议数据
│   ├── slashCommands.ts         斜杠命令解析和建议
│   ├── skillKeyFlow.ts          Skill 选择键流
│   ├── contentViews.ts          消息内容视图模式
│   ├── defaultViewChanges.ts    默认视图变化提示
│   ├── uiSettings/              UI 设置归一化和偏好合并
│   └── storage.ts               浏览器本地状态持久化边界
├── appStateMaps.ts              Provider 状态 Map 的浅层复制
├── pendingRequests.ts           待处理请求的列表状态
│   └── pendingRequests.test.ts
├── tokenUsage.ts                对话级 Token 与模型用量汇总
└── tests/                       跨模块状态合同测试
```

查找时先按数据归属选择 `session/`、`workspaceFiles/` 或 Gateway 投影，再区分“事件状态 reducer”“时间线/展示投影”和“本地存储”。根层文件只保留跨域入口或单一状态派生，不把它们误认为额外的物理子目录。

## 可修改内容

- 会话、消息、trace、事件队列等前端状态的纯函数。
- localStorage 等浏览器本地状态的小型适配。
- 不直接发起 HTTP 请求的状态整理工具。
- `workspaceFiles/`：工作区文件操作的未确认 intent 与上传 Blob 的单一 IndexedDB 存储；目标冻结和后端操作回执由既有文件树 hook 编排。

## 不可修改内容

- 不放 React 组件。
- 不放后端业务规则。
- 不直接调用后端 API。

## 规范

- 保持函数纯净、输入输出显式。
- 代码注释使用中文，专业术语除外。
- 需要副作用时优先留在 `../hooks.tsx` 或 `../api.ts`。
- `workspaceFiles/` 只保存客户端待发送、待确认记录，不充当文件内容、revision 或操作结果的业务权威；不在存储模块发起 HTTP 或轮询。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
