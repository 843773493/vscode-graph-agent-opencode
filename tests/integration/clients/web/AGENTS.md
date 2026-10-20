# 目录用途

存放纯 Web 组件/页面 harness，或浏览器前端、Gateway、后端和受控替身服务之间的集成测试。

按测试实际加载的边界组织：

```text
web/
├── components/        独立挂载组件的 Vite/Chromium 装具及其生命周期检查
├── itemized_context/  条目化上下文的页面场景与受控 Provider
├── workspace_files/   文件 owner、浏览器意图与最小 HTTP 支撑
└── *.py / *.mjs       其余 Gateway/工作区/完整页面组合场景
```

组件脚本、HTML、TSX、类型配置和专用 Python 测试相邻放在`components/`，详见该目录的`AGENTS.md`。加载完整产品页面的场景不因使用Playwright而归入组件目录；只有出现共同职责后才继续分组，不按文件数机械拆分。新运行的正式产物镜像当前测试路径，历史结果保留原路径，不建立旧源码入口或产物路径回退。

`session_catalog_outbox_idb.mjs`使用同一运行上下文管理静态HTTP监听、Chromium和context，取得句柄后立即登记，再开始下一准备步骤。任何清理失败仍尝试其余资源；执行和清理同时失败时合并保留两个原因。原生临时文件归该脚本的`runtime/`，不与IndexedDB业务事实混为一层；跨标签页持久化场景及原断言不由生命周期装具替代。真实故障回收检查归`tests/integration/harness/test_outbox_fixture_lifetime.py`。

`manual_browser_connection.mjs`的资源作用域从Chromium启动前开始，取得browser/context后立即向同一运行上下文登记关闭，再继续创建page和注册路由。准备及场景失败都释放已有资源，context关闭失败仍尝试关闭browser；执行与清理异常并存时同时保留。页面尚未取得时不采集截图，已取得页面的原诊断及业务断言保持。临时文件归该脚本的正式`runtime/`，生命周期故障检查归`tests/integration/harness/test_manual_browser_fixture_lifetime.py`，不替代完整Gateway连接场景。

`boundary_cases_display.mjs`、`partial_text_boundary_display.mjs`和`unknown_tool_outcome_display.mjs`负责历史边界的页面展示；各自持有Chromium、context及页面，复用运行上下文登记逆序释放。准备阶段也在释放作用域内，关闭一个资源失败仍关闭其余资源，执行和清理异常共同保留；截图仅在取得页面后采集。原生临时文件归各脚本的正式`runtime/`。三者共同的真实浏览器故障回收检查归`tests/integration/harness/test_boundary_display_fixture_lifetime.py`，页面业务断言仍由原场景负责，不新增一层浏览器装具。

## 可修改内容

- 浏览器性能、会话历史和网络交互集成测试。
- 真实浏览器加载局部组件 harness 的集成测试。
- 测试专用 Playwright 驱动脚本。
- `workspace_files/` 下工作区文件操作的真实 owner、持久浏览器意图与最小 HTTP 支撑；保持为明确受控边界的 Integration。

## 不可修改内容

- 没有受控服务替身的完整 Web 产品 E2E。

## 规范

- 完整产品页面场景必须通过真实 Gateway 访问；局部组件 harness 必须明确标识为 Integration。
- 服务替身和页面路由拦截必须显式记录，不能伪造成功状态。
- 运行产物必须写入 `out/tests/integration/clients/web/`。
