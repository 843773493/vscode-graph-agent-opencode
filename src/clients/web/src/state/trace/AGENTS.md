# 目录用途

`src/clients/web/src/state/trace/` 存放 trace 时间线聚合入口及其聚焦 helper。

```text
state/trace/
├── traceAggregation.ts
│   ├── isLiveConversationView       区分实时投影与历史投影
│   ├── aggregateConversationEvents 按事件顺序聚合文本、工具和 trace 条目
│   └── buildPendingStatusItem       从当前会话状态生成待处理状态项
├── toolAggregation.ts              工具调用开始、成功、失败及错误签名
└── chatResponseParts.test.ts       Trace 响应片段边界用例
```

数据流由 `traceAggregation.ts` 负责编排：`tracePayload.ts` 解析事件字段，`toolAggregation.ts` 构造工具状态，`skillKeyFlow.ts` 派生 Skill 摘要，最终返回 `timeline/timelineTypes.ts` 定义的展示项。

# 可修改内容

- 可以新增或调整 trace 事件配对、失败补全、摘要构建等纯状态转换 helper。
- 可以维护只依赖 `state` 类型和工具函数的无副作用逻辑。

# 不可修改内容

- 不在这里发起 API 请求、读取 DOM 或处理 React 渲染。
- 不在这里维护全局业务状态。

# 规范

- helper 应保持纯函数优先，输入输出使用明确类型。
- 聚合时只原地更新本次调用创建且尚未返回的对象；不得修改传入的事件或载荷。这样可以避免逐个增量重复复制累计结果，同时保持对外纯函数行为。
- 与 UI 文案强相关的展示组件逻辑仍放在 `components/`。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。

模板示例；在整理 `AGENTS.md` 时请保留此行。
