# 目录用途

`src/clients/web/src/state/messageStream/` 放浏览器前端消息流（Turn SSE 实时流）的状态派生逻辑，供 `hooks/useSessionMessageStream.ts`、`state/conversations.ts` 等消费方使用。

## 可修改内容

- 消息流协议派生类型、状态结构、快照 hydration、事件 reducer、工具事件归约与展示投影的纯函数。
- 单一职责模块之间的内部拆分与符号导出调整。

## 不可修改内容

- 不新增第二套手写 DTO 或复制既有协议派生类型；类型只能来自 `src/clients/web/src/types/` 与 `api/messageStreamSnapshot.ts`。
- 不放 React 组件。
- 不直接调用后端 API 或发起 HTTP 请求。

## 规范

- `index.ts` 是模块唯一对外入口，跨模块复用的符号必须在此可导出。
- 快照 hydration 与事件 reducer 保持互为递归的单一链路，不得复制状态归一逻辑。
- 事件 reducer 只保留事件信封校验、事件分发与终态收口；工具实体 upsert 归入 `toolReducer.ts`，activity 与 model_call 的 upsert、终态收口及 activeState 归约归入 `activityReducer.ts`，block 链路（含 block 终态收口）仍留在 `eventReducer.ts`。
- 代码注释使用中文，专业术语除外。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
