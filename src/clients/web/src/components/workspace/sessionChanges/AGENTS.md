# 目录用途

`components/workspace/sessionChanges/` 存放会话文件变更树的实现：SessionChangesTree 渲染变更集列表与选中变更集的文件变更行，并提供审查标记、刷新与打开文件的交互入口。

本目录与相邻子包的边界：

- `components/workspace/`：工作区会话级组件的其它族；本目录只承载会话变更这一条链路，其上层的「更改」标签页编排仍由 `components/workspace/WorkspaceAuxiliaryPanel` 承担。
- `components/workspace/fileTree/`：工作区文件树；本目录只展示会话变更集，不渲染工作区目录树。
- `components/panels/`：可停靠面板容器；本目录不是停靠面板，也不含面板外壳与分页编排。
- `state/display/`：展示态镜像；本目录只做变更数据的展示映射，不持有变更权威状态。

# 可修改内容

- 变更集列表、变更文件行与审查按钮的渲染与交互。
- 变更摘要文案与文件变更类型（新增/删除/修改）的图标与文案映射。

# 不可修改内容

- 不定义后端协议类型权威（来自 `types/backend`）。
- 不在本目录发起会话变更的读取、刷新或审查写请求；这些由上层通过 props 传入。
- 不吞掉错误；错误必须显式呈现。

# 规范

- 变更数据只以 props 传入的权威对象为依据，不在本目录伪造成功状态。
- 导入共享模块使用显式相对路径，禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行 `bun run --cwd src/clients/web build`。
