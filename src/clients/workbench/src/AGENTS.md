# 目录用途

Web 与 Electron 共用的唯一完整 Code OSS 工作台入口。复用同级 web 的 React 会话产品，不维护第二套外层工作台。

# 可修改内容

本目录的启动、构建、宿主和业务贡献。upstream 是固定提交的官方 Git 子模块；项目定制放在宿主及贡献中，保持上游可核验。

# 不可修改内容

不得导入旧 web 的 App 或外层工作台布局；允许复用会话 React 组件、业务 hook 与组件样式，样式必须限制在 React 宿主中；不得依赖 reference_repo；不得伪造 Gateway/后端业务状态；不得运行 upstream 的测试。

# 规范

保留完整原版 Workbench 的服务和行为，不以 Grid 仿制替代。bun 管理独立依赖；生成文件写 dist 并忽略；测试及临时工作区遵守根 AGENTS。原版子模块沿用其目录和上游指引，项目新增源码目录须有四段式 AGENTS。

来源和更改文档沿原只读内容 Provider／resolver 共享模型与释放引用；更改仅合并在途变更集读取，不缓存第二份历史正文。只读输入只序列化稳定引用，原编辑器组保存视图态；旧更改投影在唯一输入工厂一次迁移，不复用临时路由或保留旧文件 Provider。

文件及来源文档的复制引用共用 `resourceActions` 的原 Action／Command，编辑器菜单、文件菜单与 Quick Access 不另写复制逻辑。规范引用由 Gateway 唯一 URI owner 提供，包含明确 Gateway；复制不打开资源、不读取正文、不固定版本或激活来源。
