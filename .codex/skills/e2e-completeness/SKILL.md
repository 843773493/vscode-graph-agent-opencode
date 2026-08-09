---
name: e2e-completeness
description: Diagnose and implement complete browser E2E coverage across every user-facing entry point and transport boundary, including Playwright UI actions, clipboard/file chooser/drag-and-drop flows, WebSocket/Gateway/app-server evidence, deterministic fixtures, and real-provider validation. Use only when the user explicitly requests E2E work, asks to find an E2E-related bug, names this skill, or asks to validate a specific browser flow; do not use for routine development or ordinary unit tests.
---

# 完整 E2E 诊断

只在用户明确要求 E2E、Playwright、浏览器端到端测试，或要求排查某个浏览器交互 Bug 时使用。普通功能开发、单元测试和常规回归不要自动套用本技能。

## 硬性边界

- 不要把一个共享组件的通过结果当成所有入口都通过。先从源码列出真实用户入口；每个入口必须有独立的覆盖记录。
- 不要用直接 API、伪造 WebSocket 消息或直接调用内部函数替代 UI E2E。接口调用可以用于准备数据，但不能作为 UI 链路的唯一证据。
- 不要把 mock Provider 测试描述为真实模型验证。报告中明确标出 mock、确定性 Provider 和真实 Provider。
- 未执行、被跳过或只覆盖了相邻入口的路径必须报告为未验证，不能笼统声称“功能已通过”。
- 保留用户已有改动；检查 `git status`，只修改任务相关文件，不使用宽泛的 reset 或 checkout。

## 工作流

### 1. 还原用户动作

把用户描述拆成可执行动作：页面/路由、控件、输入方式、发送动作、目标设备、后台服务、模型类型和预期可见结果。优先从源码、路由和现有测试确认，不凭控件名称猜测实现是否共享。

特别区分这些常见但不等价的入口：

- 普通聊天框的文件选择器；
- 普通聊天框直接粘贴图片或文件；
- 普通聊天框拖拽；
- 插件、欢迎页、创建页或其他独立页面上的附件按钮；
- 桌面、移动 Web、Electron/Tauri 等不同运行时。

### 2. 建立覆盖矩阵

先写一个临时或测试报告内的矩阵，至少包含：

| 入口 | 页面/运行时 | 浏览器动作 | UI 结果 | 传输证据 | Provider/模型证据 |
| --- | --- | --- | --- | --- | --- |

只有在矩阵中逐项确认后，才能宣称某个范围完成。若用户只要求一个入口，仍要说明相邻入口没有被本次测试覆盖。

### 3. 选择分层测试

- 组件测试：验证回调接线、按钮状态、上传中/失败状态和无障碍标识。
- 确定性浏览器 E2E：使用真实 Chromium/Playwright、真实文件选择器、键盘和粘贴事件，验证页面到 Gateway/app-server 的协议链路。
- 真实模型 E2E：在凭据和环境允许时，验证 Provider 请求确实含有图片/文件内容，并让模型输出不可歧义的确认标记。

复杂功能至少需要一条确定性浏览器路径；涉及模型理解、附件格式或 Provider 适配时，再增加真实模型路径。不能只凭组件 mock 测试交付。

### 4. 采集链路证据

测试必须在适当边界收集证据，而不是只检查最终文字：

1. UI 是否出现预览、上传完成和错误状态；
2. 文件选择器、粘贴或拖拽事件是否实际触发；
3. 上传请求是否成功；
4. WebSocket 的 `turn/start` 或等价消息是否包含附件 envelope、文件名或附件 ID；
5. Gateway 转发到 app-server 后，Provider 请求是否包含对应的图片/文件内容；
6. 用户消息和历史刷新后是否展示附件；
7. 页面错误、请求失败和 WebSocket 错误是否为空。

对敏感数据只保存字段、类型、长度、文件名和布尔证据，不把 token、完整图片或 Provider 响应秘密写入日志。

### 5. 覆盖状态和边界

按功能选择相关项，但至少检查：

- 上传期间不能发送；
- 上传失败后给出可见错误且不能误发送；
- 只有附件、没有文字时的发送；
- 发送后附件出现在用户消息；
- 刷新、重连或切换页面后的历史恢复；
- 旧连接断开时新连接不会抢占未完成的线程或丢失附件；
- 任何相邻入口是否拥有不同的事件接线。

### 6. 运行和报告

运行最小相关测试后，再运行该变更影响到的完整测试集。报告必须包含：

- 覆盖矩阵或等价的逐入口清单；
- 每条路径使用的真实浏览器动作；
- mock/确定性 Provider/真实模型的区分；
- 关键传输证据；
- 命令、通过数、跳过数和未验证项；
- 若某入口无法测试，说明原因和剩余风险。

## 当前项目约定

- Playwright E2E 位于 `apps/kunlun-studio/e2e/`，先查看 `suite-registry.mjs`、`harness/chromium.mjs`、`harness/gateway.mjs` 和现有相邻 suite。
- 前端测试位于 `apps/kunlun-studio/wework/src/**/*.test.tsx`；优先使用已有 `data-testid`，不要用脆弱的 CSS 结构选择器。
- 图片/文件附件必须分别验证文件选择、粘贴和其他实际支持的入口；不能因为它们最终都调用 `handleFileSelect` 就省略浏览器动作。
- 涉及 TUI 时同时加载 `kunlun-tui-lab`；涉及真实 TUI 用户体验时按用户要求加载 `kunlun-tui-user-reviewer`。
