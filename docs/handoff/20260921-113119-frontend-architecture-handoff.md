# 前端修复与架构整理交接

- 交接时间：2026-09-21 11:31:19 UTC+8
- 仓库：`/data/hyf/20260629_agent/vscode-graph-agent-opencode`
- 分支：`main`
- 当前 HEAD：`2067b2a refactor(web): 让消息流快照直接进入强类型 reducer`

## 本阶段目标与约束

本阶段持续修复浏览器前端、拆分大文件、清理正常运行路径中的 legacy 双轨，并用严格 DTO、生成协议类型和唯一解析入口减少字段猜测。实现遵循以下约束：

- 旧版本数据允许直接失败，不为旧格式保留正常运行兼容层。
- 每次只推进一条纵向链路，完成测试后独立提交。
- 不覆盖或提交用户已有工作树改动。
- Web 代码修改后必须运行 TypeScript 静态检查和生产构建。
- 临时浏览器与 E2E 工作区、截图和日志只能放在 `out/tests/temp/<task_name>/`。
- 本阶段用户要求使用 Luna Max 子代理；后续若继续委派，仍应先核对用户最新模型要求。

## 已完成提交

### 最新停点

- `2067b2a refactor(web): 让消息流快照直接进入强类型 reducer`
  - HTTP snapshot 直接调用 `applyMessageStreamSnapshot`，不再伪装成 SSE synthetic event。
  - SSE `stream.snapshot` 在唯一 API 解析入口校验为严格平铺 snapshot payload。
  - HTTP snapshot 与 SSE snapshot payload 使用同一生成协议来源派生的类型，未新增第二套手写 DTO。
  - reducer 不再对 snapshot 顶层数组、标量和身份字段做 `Record<string, unknown>` 猜测。
  - 40 个消息流 focused 测试通过。

- `d93e4cd refactor(web): 拆分主工作区布局拖拽`
  - 将主窗口横向三栏与扩展调试 split 的 pointer resize、监听器清理移入 `useMainAreaResize.ts`。
  - 比例计算继续放在 `layout/workbenchLayout.ts` 的纯函数中。
  - `App.tsx` 从约 2032 行降至 1898 行。
  - 底部面板纵向拖拽留在 `App.tsx`，没有扩大本切片职责。

- `bb62003 refactor(debug): 统一 Node 调试动作历史上限`
  - `MAX_NODE_DEBUG_ACTIONS` 成为动作历史唯一上限。
  - 求值历史改用独立的 `_MAX_NODE_DEBUG_EVALUATIONS`。
  - 删除 service 与 session state 内重复的 `_MAX_ACTIONS`。

### 此前同一轮已完成的重要提交

- `522b681 修复消息流快照强类型契约`
  - 后端新增严格 Pydantic snapshot DTO。
  - 删除伪终态 `_minimal_terminal_snapshot` 降级。
  - 前端 snapshot wire validator 与协议覆盖落地。

- `6199b2f refactor(debug): 统一 Node 调试会话状态持久化`
  - `NodeDebugSessionState` 成为 runtime projection 与 manifest 持久化的唯一职责入口。

- `71c8f2e refactor(web): 删除旧版界面设置双轨`
  - 删除正常运行路径中的旧 UI 设置文件读写、旧 tab 值和字段修补。
  - UI 设置 DTO 使用严格字段校验；显式维护迁移只归档旧文件，不导入当前 profile。

- `e40b475 重构 Gateway 工作区生命周期 Hook`
  - 将 Gateway 工作区生命周期从 `hooks.tsx` 移至 `useGatewayWorkspaceRuntimeLifecycle.ts`。

- `40b9ab2 refactor: 统一 Node Debug 启动方案解析`
  - fork 与正常启动统一使用 `runtime_config.py`，删除 `launch_profiles` 独立猜字段路径。

- `a5a2de3 refactor: 将 rollout 格式闸门移出迁移模块`
  - 正常 runtime 不再导入 migration dispatch。

- `2bcb575 fix(rollout): split parallel tool call canonical items`
  - 修复并行工具调用 canonical item 分组与封存行为。

- `5f6ece9 refactor: 拆分 Node Debug 配置与会话状态`
  - 拆分配置注册、会话状态、启动编排和快照职责。

- `d195014 refactor: remove startup legacy storage migrations`
  - 删除正常启动路径中的旧存储自动迁移，保留显式用户存储维护入口。

## 最新验证结果

在 `2067b2a` 与 `d93e4cd` 均提交后，主代理重新运行并通过：

```text
bun run check:protocol
bun x tsc --noEmit -p src/clients/web/tsconfig.json
bun run --cwd src/clients/web build
bun test \
  src/clients/web/src/hooks/useSessionMessageStream.test.tsx \
  src/clients/web/src/state/tests/messageStream.test.ts \
  src/clients/web/src/state/responseParts.test.ts \
  src/clients/web/src/api/messageStreamSnapshot.test.ts
bun src/clients/web/src/state/tests/workbenchLayout.test.ts
```

结果：

- 协议检查通过。
- TypeScript 无错误。
- Web 生产构建通过。
- 消息流 focused 测试 `40 passed, 0 failed`。
- 布局比例断言通过。
- 构建仍有既有的大 chunk 警告：`main2.js` 约 1.24 MB；本阶段未处理代码分包。

更早的统一后端检查曾达到 `337 passed`，但它发生在最新两个 Web 提交之前。最新提交只改 Web，本交接不把该历史结果表述为最新全仓重跑。

## 真实浏览器审查状态

最终只读审查使用：

```text
frontend=http://127.0.0.1:8060
BOXTEAM_DEV_PORT_OFFSET=49
BOXTEAM_HOME=out/tests/temp/frontend_stop_boundary_20260921/runtime
workspace=out/tests/temp/frontend_stop_boundary_20260921/workspace
artifacts=out/tests/temp/frontend_stop_boundary_20260921/artifacts
```

已验证：

- 完整开发服务可启动，初始页面可加载，标题为 BoxTeam。
- `/api/gateway/health` 与 `/api/gateway/workspaces` 返回 200，响应体和响应头均带 `request_id`。
- 未携带 Gateway 凭据请求 `/api/v1/workspace` 返回 401，符合鉴权边界；未验证带浏览器凭据后的 200。
- 通过可见 UI 进入 Chats 并点击“新建会话”，成功显示新会话空状态和输入提示。
- 初始页面与新会话阶段未观察到 `console.error` 或 `pageerror`。
- 主窗口两个横向分隔条可见，并带明确的 title 与 aria-label。

未验证：

- 没有发送消息，因此未完成即时态、中间态、最终态和 snapshot 刷新恢复的真实浏览器验证。
- 未验证 reasoning/tool 原文泄漏、重复卡片、取消或重试。
- 未实际拖动分隔条，也未验证刷新后的布局持久化。
- 不能仅凭本次部分审查宣称核心聊天和拖拽无回归。

审查服务已停止并确认 `inactive/dead`。保留证据：

- `out/tests/temp/frontend_stop_boundary_20260921/artifacts/session-created.png`
- `out/tests/temp/frontend_stop_boundary_20260921/artifacts/chats-new-session.png`

没有生成 Playwright trace。

## 必须保护的工作树改动

当前工作树只剩下列用户已有改动，后续不得 reset、restore、覆盖或混入重构提交：

```text
 M app/gateway/control/generators.py
 M app/services/business/session_generation/service.py
?? examples/demos/Itemized_context_storage/
```

开始新工作前先运行：

```bash
git status --short
git diff --check
```

## 当前架构残余

按停点时统计：

```text
1898 src/clients/web/src/App.tsx
1444 src/clients/web/src/hooks.tsx
1538 src/clients/web/src/state/messageStream.ts
1534 app/services/infrastructure/node_debug/service.py
 450 app/services/infrastructure/node_debug/configuration_registry.py
 286 app/services/infrastructure/node_debug/session_state.py
```

这些文件仍超过架构审查阈值。最新 snapshot 改造消除了字段猜测，但 `messageStream.ts` 因显式 typed hydration 增长到约 1538 行；后续应按事件 reducer、snapshot hydration、展示投影等真实职责拆分，不能只搬代码或复制 DTO。

目录层面，`src/clients/web/src/state/`、`src/clients/web/src/hooks/` 与 `app/services/infrastructure/node_debug/` 的直接源码文件数量仍超过 20。新增模块前应优先评估已有子目录或按职责下沉；新源码子目录必须包含自己的 `AGENTS.md`。

## 已识别但未开始的后续切片

下列任务没有代码落盘，不要假设已经完成：

1. **补齐真实浏览器回归**
   - 从隔离工作区通过 UI 新建会话并发送消息。
   - 分别观察发送后的即时态、中间态和最终态。
   - 刷新页面验证 snapshot 恢复；再验证一次取消或重试。
   - 实际拖动主窗口横向分隔条，验证释放与刷新后的状态。
   - 检查 console/pageerror、重复卡片、reasoning/tool 原文泄漏、`Maximum update depth` 和 `seal-dispatch-tool-pairing-conflict`。

2. **删除 Gateway `source_owner="legacy"` 正常运行双轨**
   - 当前 `RemoteGatewayConnection.source_owner` 仍允许 `"config" | "manual" | "legacy"`，注册表恢复会把缺失或非法 owner 默认成 `legacy`。
   - 应先全仓检索构造、持久化、batch、projection、runtime retirement 和测试，再把合同收紧为 `"config" | "manual"`。
   - 旧数据应明确失败，不得再默认、自动归一或添加兼容 adapter。
   - 此切片曾委派但在写代码前中止，工作树无相关修改。

3. **继续拆分 `NodeDebugService`**
   - 候选是 `_source_digests_for_runtime` 与 `_reconcile_session_sources` 这条源码/断点对账链路。
   - 优先复用现有 `NodeDebugBreakpointMutations` 与 `breakpoints.py`，避免在已经拥挤的目录继续横向新增文件。
   - 另一候选是 evaluation、stream reader 与 process monitor，但必须各自作为独立纵向切片。
   - 该任务曾委派但因无进度被中止，工作树无相关修改。

4. **继续拆分前端大文件**
   - `hooks.tsx` 可继续抽取 Gateway 工作区 mutation、session selection 或 bootstrap 单链路。
   - `App.tsx` 可继续抽取纯布局/视图编排，但不能把整个 AppContext 作为参数传给新 hook。
   - `messageStream.ts` 应优先拆 typed snapshot hydration 和事件 reducer，保持唯一类型来源。

## 建议恢复顺序

1. 检查 HEAD、工作树和受保护改动。
2. 先补齐真实浏览器关键聊天、刷新恢复和拖拽验证；若发现 bug，完成修复、focused 测试和复验后独立提交。
3. 再选择 Gateway legacy owner 删除或 Node Debug 源码对账中的一个纵向切片，不要并行触碰同一文件。
4. 每个 Web 提交至少运行 `bun x tsc --noEmit -p src/clients/web/tsconfig.json` 与 `bun run --cwd src/clients/web build`。
5. 每个 Node Debug 提交运行 `uv run ruff check`、`uv run compileall` 和对应 focused tests。
