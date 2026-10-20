# 目录用途

存放独立挂载真实 Web 组件的 Vite/Chromium 集成装具。这里不启动完整 Gateway/工作区业务链，附件请求使用明确的 HTTP 替身。

```text
components/
├── anchored_overlay_positioning.mjs             浮层定位场景
├── anchored_overlay_positioning_harness.*       相邻 HTML/TSX 入口
├── tsconfig.anchored_overlay_positioning.json   浮层装具及真实组件的类型检查
├── structured_user_message_attachments.mjs      附件展示、预览及失败反馈场景
├── structured_user_message_attachments_harness.* 相邻 HTML/TSX 入口
├── tsconfig.structured_user_message_attachments.json  附件装具及真实组件的类型检查
├── test_structured_user_message_attachments.py  Python 场景入口
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
