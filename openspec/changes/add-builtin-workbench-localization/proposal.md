## Why

当前完整工作台保留英文 Code OSS 文案，复用的 React 会话界面及项目定制多为硬编码中文；HTML 的语言声明并未接入真实 NLS 加载，导致同一页面中英文混用。产品需要开箱即用的简体中文和离线可用的英文，并沿 VS Code 的启动时语言机制统一切换。

## What Changes

- 内置 `zh-cn` 与 `en`，没有显式选择时固定使用简体中文，不按浏览器或操作系统语言自动改选；清除偏好返回中文。
- 复用原显示语言命令、Quick Pick 与客户端语言偏好 owner；用户选择后保存并提示重载，Web 重载页面、Electron 重载同一 renderer，不重启 Gateway、Backend 或插件。
- 在 Workbench 及 React 的业务模块求值前加载本地语言资源，统一 NLS、产品翻译入口、HTML lang 和格式化语言；启动提示也使用同一选择。
- 在现有构建和源码转换接缝接入 Code OSS NLS 提取及翻译资源生成；锁定上游中文翻译数据及来源，不运行汉化 VSIX、不恢复市场或用户 Extension Host。
- 将项目实际使用的 React、Workbench 定制、状态标签、菜单、设置、Tooltip 和无障碍文案纳入中英文资源；共享翻译逻辑不依赖 VS Code／Electron／浏览器全局对象，不建立第二语言偏好或热切换框架。
- 覆盖随产品交付的内置扩展清单及运行期翻译、实际使用的 Worker／Webview，全部资源随 Web／Electron 产物分发；开发和发布使用同一翻译身份与规则。
- 保留用户消息、模型回复、Skill／提示词、文件、原始日志和工具结果；错误码、URI、命令／配置 key 及业务请求合同不因语言变化改变。
- 以少量真实产品场景验证默认中文、切换英文后重载、清除恢复中文、离线运行与展示状态恢复；本轮仅编写规划，不实施产品代码或修改用户环境。

## Capabilities

### New Capabilities

- `workbench-localization`：中英文内置资源、启动语言解析、统一显示语言选择与重载、产品及内置功能翻译、离线构建和状态保持验收。

### Modified Capabilities

无。沿用 `native-web-workbench`、`electron-thread-history` 的工作台、重载保护及桌面共享边界；本变更以新能力补充本地化，不改写它们的既有行为合同。

## Impact

- Workbench：`src/clients/workbench/index.html`、启动入口、原 Locale／LanguagePack 服务接缝、源码转换、Vite 预构建和发布构建。
- 产品组件：`src/clients/web/src` 中实际使用的组件、hook、状态展示及格式化；共享纯翻译与资源逻辑放入 `src/clients/shared/core`，不反向依赖工作台入口。
- 发行：内置扩展翻译、相关 Worker／Webview 资源、Web 静态资源服务及 Electron renderer；Electron 仍只承担现有窗口／生命周期／固定存储，不新增原生系统能力或独立翻译服务。
- 验证：复用现有 Workbench／Gateway／Workspace 和真实 Electron 装具，增加本地化合同及语言切换场景；不默认全仓长跑，不为 UI 翻译改造整个后端错误协议。
