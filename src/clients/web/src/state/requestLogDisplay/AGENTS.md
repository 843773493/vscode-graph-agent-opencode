# 目录用途

`src/clients/web/src/state/requestLogDisplay/` 放浏览器前端「请求日志展示族」的纯派生逻辑：把后端权威的 LLM 请求日志记录（`LLMRequestLogRecord`）投影成请求日志面板可直接渲染的展示模型、文案与归一结果，供 `components/panels/RequestLogPanel.tsx` 消费。本子包直接继承旧顶层文件 `src/clients/web/src/state/requestLogDisplay.ts`，其全部导出符号与外部契约保持不变（`index.ts` 是对外唯一入口）。

与相邻子包的边界：

- `state/display/`：放大盘权威数据（Agent State、工具事件、资源、事件队列）的展示投影。本包同样是「权威数据 → 展示模型」的单向投影，但输入源专指请求日志记录，二者互不内联复制归一逻辑。
- `state/trace/`：Trace 事件的配对、失败补全与聚合。请求日志里的 `tool_calls` 配对只服务于「本条日志/本组日志」的条目展示与关键流转聚合，不参与 Trace 事件时间线的配对，也不产出 Trace 聚合结果。
- `state/messageStream/`：Turn SSE 实时实体状态与快照 hydration。本包只读已落盘的历史日志，不读写任何流状态。
- `state/skillKeyFlow/`、`state/customTools/`：分别提供 skill 关键流转状态机器与 `invoke_extension_tool` 协议解析。本包单向消费它们，不复制其常量与解析规则。

## 可修改内容

- 请求日志概览、上游尝试、请求重放、关键流转等展示模型与文案映射的纯函数。
- 请求日志 JSON 的展示归一分支与合并规则。
- 本包内部的模块拆分与符号导出调整（对外契约仍由 `index.ts` 统一收口）。

## 不可修改内容

- 不新增第二套手写 DTO 或复制既有协议派生类型；类型只能来自 `src/clients/web/src/types/` 或本包 `types.ts` 的展示模型。
- 不放 React 组件，不发起 HTTP 请求、读取 DOM 或处理副作用。
- 不生成或回写权威数据（请求日志记录本身、Trace 事件、流状态）。

## 规范

- 纯函数优先，输入输出显式；非法或缺失字段按既有边界收敛为明确的未知值，不抛到 UI 层。
- 展示文案属于用户可见契约，新增或修改文案必须同步 `state/tests/requestLogDisplay.test.ts`。
- 与 `display/`、`trace/` 保持单向分工，不得互相内联复制归一逻辑。
- 代码注释使用中文，专业术语除外。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”
