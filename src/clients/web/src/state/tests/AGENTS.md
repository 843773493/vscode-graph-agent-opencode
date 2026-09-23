# 目录用途

`src/clients/web/src/state/tests/` 是状态派生层测试中尚未归位的暂存目录。

按既有惯例，测试应与被测生产模块就近同目录存放（例如 `state/session/sessionScope.test.ts`、`state/timeline/responseParts.test.ts`、`state/display/toolDisplay.test.ts`）。本目录只保留职责跨域、无法归入单一生产子域的用例；新增测试应直接放进被测模块所在目录，不要再平铺到这里。

# 可修改内容

- 仅维护职责跨域、暂时无法就近归位的存量测试文件。
- 可以添加小型测试数据构造 helper，但应保持用例聚焦。

# 不可修改内容

- 不在这里放 React 组件测试。
- 不在这里直接访问后端 API、DOM 或浏览器状态。
- 不在这里放产品运行时代码。

# 规范

- 单个测试文件只覆盖一个主要状态模块或一条明确的跨模块链路；能归入单一生产子域的用例必须就近存放，不得滞留本目录。
- 新增测试优先复现真实日志/事件结构，尤其是 LangChain 消息 `id` 与 `tool_call_id` 同时存在的情况。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
