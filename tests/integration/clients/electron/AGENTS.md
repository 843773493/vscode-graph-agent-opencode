# 目录用途

真实 Electron 原生容器与共享 Workbench、Gateway、工作区后端的集成回归。`test_packaged_workbench.py` 构建并校验便携归档，`packaged_workbench.mjs` 从解压后的 ASAR 产物操作会话、虚拟文件和重启恢复；`seed_electron_cache.mjs` 通过当前官方下载库校验并导入已有运行时 ZIP。

`image_cache.mjs` 在同一 ASAR 原图片编辑器上通过 CDP 核验首次200、再次及正常退出重启后的304／零正文和独立profile隔离，只记录选定条件头、request_id和字节量，不采集凭据。图片来自本测试隔离工作区，不替换图片Pane，不调用缓存flush或文件系统IPC；此证据不覆盖原Webview缓存。

`readonly_documents.mjs` 通过真实 Agent 的 write_file 建立更改，从原会话资源打开 Diff；核对分屏共享、只读、实际关窗重启后的 Diff／来源恢复及独立 profile 隔离。正文继续来自原内容 Provider，只观察实际读取，不预写变更集或浏览器存储；统一 model_stream 的工具调用和后续响应必须在原始模型记录中配对。

`launch.mjs` 为规范文件与 Webview 场景共用真实 ASAR 启动、同 profile／来源重启、正常关窗及日志释放；每个调用方显式传入自己的运行上下文。`test_webview_resource_cache.py` 复用原 Webview 的 CSS／字体／更新／定向淘汰断言，同时以 CDP 核原图片首次正文和全部后续304的零正文、相同归属及独立request_id；不预写缓存或手动flush。

# 可修改内容

- Linux 原生 Electron 的产物构建、窗口操作、独立 profile、固定页面来源和缓存恢复验收。
- 明确的模型传输回放及真实请求延迟装具。

# 不可修改内容

- 不用 electron-web 或 Chromium 页面替代原生 Electron，不伪造 Gateway／Backend 响应。
- 不向 renderer 暴露 Node、文件系统 IPC 或原生扩展能力。

# 规范

- 通过正式 `package:electron` 入口构建；使用 `--output-root` 将全部产物放入本测试镜像的 runtime，校验归档摘要后解压启动。
- Electron 使用独立 cwd、空 PATH 和独立 BOXTEAM_HOME；不读取其它测试的会话或应用 bundle。已有官方 Electron 下载缓存可以复制加速，仍由打包器校验固定版本摘要。
- 首窗口句柄不代表根文档就绪；先按现有页面操作预算等待默认根 URL 的 DOMContentLoaded，再执行控件断言。最终独立 HTTP 历史审计使用正常 Gateway 用户认证，不能用本地凭据替代用户会话。
- 通过真实 Gateway、Backend、Agent 和精确模型回放验证业务，分类为 Integration；HTTP 延迟代理只转发真实响应。
- 正常退出必须经过真实 BrowserWindow.close，并等待进程退出；不得手动调用缓存 flush 冒充原生关闭链路。
- 每次启动保存 renderer 的原始 console error；正常关窗和重启的 IndexedDB 写入错误必须失败，不能归为原版 shutdown joiner 日志。
- 延迟代理在客户端响应关闭时销毁对应上游请求，正常窗口关闭也必须释放 SSE；只关闭监听和客户端 socket 不能证明上游已释放，不用强制退出驱动掩盖保活连接。
- 回放控制服务必须晚于 Backend 关闭，附件运行时释放仍会使用受控时钟；业务驱动退出后还要核对 Backend 的正常关闭日志，不能以 pytest 成功掩盖 ASGI 关闭失败。
- 切换 Turn 变更集后先等待对应按钮的 `aria-pressed` 确认，再打开文件；旧列表仍可见不能证明异步选择已完成。Diff 的实际读取次数和完整 Gateway／Workspace 归属继续严格核对。
- Linux 当前以 Xvfb 和显式 `--no-sandbox` 启动，记录实际参数；WebPreferences 的 sandbox 配置不证明 Chromium 进程沙箱已启用。Windows／macOS、真实 Provider 和崩溃恢复另行验收。
- 正式工作区、profile、日志、截图和归档只写 `out/tests/integration/clients/electron/test_packaged_workbench/`，按 run／node 隔离；矩阵提供外部超时和 4GiB 数据段保护。
- 打包和原生驱动都必须登记完整进程组 owner，超时也回收子进程并保存实际退出回执；不得直接复制旧下载缓存的散列目录假设当前下载库仍使用同一键。
- 打包开始前通过原 E2EProcessLease 预留 Backend、Gateway 和延迟代理整组端口，服务全部关闭后释放；不让启动前的网络请求抢占后续服务端口。
- 构建和依赖仍使用 Bun。原生 Playwright Electron inspector 驱动暂用 Node：Bun 1.3.11 下连接不完成，同一 ASAR 在 Node 下可连接；保留 TODO 和原始对照证据，不增产品兼容层。Xvfb 等工具的临时目录也放入本次 runtime。
