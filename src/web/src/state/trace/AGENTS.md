# 目录用途

`src/web/src/state/trace/` 存放 trace 时间线聚合入口及其聚焦 helper。

# 可修改内容

- 可以新增或调整 trace 事件配对、失败补全、摘要构建等纯状态转换 helper。
- 可以维护只依赖 `state` 类型和工具函数的无副作用逻辑。

# 不可修改内容

- 不在这里发起 API 请求、读取 DOM 或处理 React 渲染。
- 不在这里维护全局业务状态。

# 规范

- helper 应保持纯函数优先，输入输出使用明确类型。
- 与 UI 文案强相关的展示组件逻辑仍放在 `components/`。
