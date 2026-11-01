# 目录用途

存放独立挂载真实 Web 组件的 Vite/Chromium 集成装具。这里不启动完整 Gateway/工作区业务链，附件请求使用明确的 HTTP 替身。

```text
components/
├── anchored_overlay_positioning.mjs             锚定浮层、文件树提示与搜索行可达性场景
├── anchored_overlay_positioning_harness.*       相邻 HTML/TSX 入口与文件树控制
├── tsconfig.anchored_overlay_positioning.json   浮层装具及真实组件的类型检查
├── structured_user_message_attachments.mjs      附件展示、预览及失败反馈场景
├── structured_user_message_attachments_harness.* 相邻 HTML/TSX 入口
├── tsconfig.structured_user_message_attachments.json  附件装具及真实组件的类型检查
├── test_structured_user_message_attachments.py  Python 场景入口
├── react_surface.mjs / react_surface_harness.*  真实 ReactSurface 双根错误、浮层、样式及释放隔离
├── tsconfig_react_surface.jsonc                 ReactSurface 装具及真实依赖的类型检查
└── test_component_fixture_lifetime.py           准备失败时真实句柄的释放检查
```

同一场景的脚本、页面、类型配置和专用测试保持相邻；真正跨场景复用的运行设施归`tests/harness/`，不复制业务逻辑或再建资源管理框架。

# 可修改内容

- 局部组件的真实浏览器场景、相邻 HTML/TSX 入口和类型检查配置。
- 组件场景的显式 HTTP 替身与装具生命周期检查。

# 不可修改内容

- 不放完整产品页面、Gateway 路由或工作区后端场景。
- 不在测试中实现产品业务逻辑，不通过放宽原断言掩盖问题。
- 不保留上层旧源码路径转发或旧产物目录回退。

# 规范

- 资源作用域覆盖准备和断言全过程：取得 Vite 后立即登记关闭，再开始监听；取得 Chromium 后立即登记关闭，再创建 context/page。准备失败同样由外层`finally`逆序清理，已有页面的业务失败截图留在场景作用域。
- 原生临时文件与 Vite 缓存归`out/tests/integration/clients/web/components/<脚本名>/runtime/`，不写源码树内的`node_modules/.vite`或依赖系统临时目录；其他正式结果同样镜像当前测试路径。
- 故障测试只在准备边界注入错误，先观察装具的释放结果，再由探针兜底回收真实句柄；不能把该检查称为产品 E2E。
- 类型检查加载真实组件及其依赖。移动装具时同步导入、类型配置、Python 入口和 suite 注册，保留原用例与业务断言。
- Vite 组件装具直接加载源码，专用 Python 入口不先生成整站`dist`。装具的语义类型检查使用相邻`tsconfig`；产品前端改动仍按根目录规范完成相应类型检查和构建。
- 浏览器启动与路径选择归 Node 装具：显式`PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH`原样透传，未配置时使用 Playwright 默认浏览器。Python 入口不二次搜索系统浏览器，避免同一场景因入口不同切换到另一套 Chromium。
- `react_surface.mjs` 直接挂载产品的 ReactSurface、完整 productStyles 及五个资源标签，不使用旧 App/main 或复制错误边界。显式触发缺失 AppContext 与 Provider 初始化抛错，逐条归因预期 React 诊断，未知页面错误及请求失败仍失败；核对另一根保持状态、浮层归属、宿主焦点、释放以及外层样式不变。只为无会话的文件树提供已声明的只读授权／文件根 HTTP 替身，拦截必须先核 `/api/` 前缀，不得拦截源码目录内的 api 模块。它不装配 Gateway、Thread 控制器或模型，不替代完整页面及业务失败通道验收。
  - Vite 显式去重并预构建 React 与 renderer 入口，避免跨目录或依赖符号链接导致初次挂载期间晚发现依赖、改变模块查询版本；保持原双根、错误、浮层及样式断言，不通过刷新页面或放宽等待掩盖多实例。
  - 正式 ReactSurface 矩阵与产品 Vite 入口统一使用 Node；Bun 启动对照停在 Vite listen，Node 相同准备代码能够创建浏览器并释放。保留原90秒／4GiB及全部业务断言，不按运行失败自动切换运行时；准备故障探针仍以 Bun mock 验证同一脚本的资源释放边界，不替代正式 Node 浏览器验收。
- 主题场景另声明精确图片 HTTP 替身，覆盖真实宿主背景 CSS、404 警告、过期成功／失败和释放取消；等待实际主题 Promise 完成，不能只等 HTTP fulfill 就认定图片回调结束。通知使用明确的装具 collector，仅证明宿主发布边界，不替代完整工作台的原版通知服务接线验收。图片 404 console 错误按唯一 URL 和原文逐条归因，未知错误仍失败。
