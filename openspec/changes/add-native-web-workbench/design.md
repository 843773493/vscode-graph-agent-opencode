## Context

现有 web 使用 React 工作台，独立技术验证已证明原版 Grid/EditorGroupModel 能承载三个精确 Thread，但没有完整设置、标签栏与命令体系。此次直接使用完整 Code OSS Web Workbench，不沿用试验宿主。

## Goals / Non-Goals

Goals: 原版文件交互、设置及恢复；独立源码与构建；逐步接入精确工作区/Session/Thread 和编辑器内资源。Non-Goals: 微软发行版外部服务、桌面原生能力、在本次验收前替换旧入口。

## Decisions

- src/clients/workbench 是独立客户端；upstream Git 子模块固定官方提交 fc8954c90fe6dea1f4c411dd73fdfe5f1913b5ca。gitlink 与 .gitmodules 纳入根版本控制，源码对象独立于 reference_repo。定制贡献保存在主仓，不修改未跟踪的参考源码。
- 使用完整 workbench.web.main.internal 入口及其配置、存储、编辑器、命令、主题、扩展服务。独立构建输出资源与 Worker，不用旧 React App 包壳。依赖按上游 Web 目标维护，用 bun 安装。
- 普通文件通过工作台文件服务连接浏览器持久文件系统；后续工作区文件和会话导航通过显式 Gateway API，不建立业务第二事实源。设置使用原版 profile/configuration/storage 服务，与旧界面展示状态隔离。
- 会话导航、Thread EditorInput/EditorPane、编辑器内资源是项目贡献。线程身份包含后端工作区 UUID、Session ID、Thread ID；Gateway 路由 ID 单独保存。预览、固定、拖拽与分组由原版服务管理。
- 按三个纵向批次验收：完整文件工作台 → 真实 Gateway 会话导航与 Thread → 编辑器内资源及多 Thread 隔离。测试不注册项目根工作区，所有探针和回放在任务隔离目录运行。

## Risks / Trade-offs

- 全量 Workbench 资源和 Worker 多 → 保留原入口和真实加载诊断，检查原始网络错误，不能以页面壳成功代替产品验收。
- Web 与桌面能力有差异 → 验收以固定提交的原版 Web 行为为准；本地进程功能必须接真实宿主服务。
- 上游升级成本 → 子模块固定提交，定制贡献独立，构建校验固定版本与资源清单。
- 双客户端过渡 → 明确独立验收入口；完成业务迁移后一次性切换，不增加旧布局适配层。该后续切换不提前更改现有用户契约。
