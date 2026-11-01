# 目录用途

Web 与 Electron 共用的唯一完整 Code OSS 工作台入口。复用同级 web 的 React 会话产品，不维护第二套外层工作台。

# 可修改内容

本目录的启动、构建、宿主和业务贡献。upstream 是固定提交的官方 Git 子模块；项目定制放在宿主及贡献中，保持上游可核验。

# 不可修改内容

不得导入旧 web 的 App 或外层工作台布局；允许复用会话 React 组件、业务 hook 与组件样式，样式必须限制在 React 宿主中；不得依赖 reference_repo；不得伪造 Gateway/后端业务状态；不得运行 upstream 的测试。

# 规范

保留完整原版 Workbench 的服务和行为，不以 Grid 仿制替代。bun 管理独立依赖；生成文件写 dist 并忽略；测试及临时工作区遵守根 AGENTS。原版子模块沿用其目录和上游指引，项目新增源码目录须有四段式 AGENTS。

`server.mjs` 是 Electron 与独立静态服务的同一 HTTP／SSE／WebSocket 代理；响应关闭时释放对应 HTTP 上游请求，不等待已经结束的请求体再次触发 aborted，也不能关闭其它 owner 的连接。传输边界验证归 `tests/integration/clients/web/workbench_server.test.mjs`，不替代真实 Gateway 或原生宿主验收。

`sourceTransform.mjs` 同时服务正式构建和 Vite。日志及设置共用的 IndexedDB 连接随浏览器文档释放，不能在同步 shutdown 开始时主动关闭并中止仍在提交的日志；不得屏蔽存储错误或替换持久日志。正式元数据页面回归同时验证真实刷新恢复与受控生命周期后的日志提交。

`dev.mjs` 为统一开发栈提供 JSON `/health`；`--outdir` 沿用构建入口的静态资源输出语义，`--cache` 只指定 Vite 派生缓存目录。正常开发默认使用本目录 dist 和 Vite 默认缓存，正式测试必须显式传入自己 runtime 下的输出／缓存目录；两者都不能改变业务工作区或用户配置。

Vite 依赖扫描只从本产品 index.html 开始，不扫描上游样例／测试 HTML；源码转换与依赖预构建共用参数装饰器及类字段赋值语义，不以禁用扫描或放宽浏览器期限掩盖冷启动失败。

开发服务先通过 Vite client environment 转换产品入口及其静态依赖图，health 等待预热完成后才返回 200／ok。业务源码仍交给同一 Vite 转换与热更新，不用发布 bundle 回退代替开发入口。

扩展 staging／发布的目录过滤仅检查复制源内部的相对路径；源码祖先目录名不得影响结果，路径分隔符使用当前平台约定。`extensions.test.mjs` 用真实文件复制验证祖先目录、内部测试／依赖排除及上游原件不变；不代替完整扩展编译与 Workbench 页面验收。
