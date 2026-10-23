# 目录用途

`components/workspace/fileTree/` 存放工作区文件树族的实现：主组件 WorkspaceFileTree、右键菜单渲染 WorkspaceFileTreeContextMenu、右键菜单动作 hook useWorkspaceFileTreeContextMenu、目录缓存与懒加载 hook useWorkspaceFileTreeDirectories、路径语义纯逻辑 workspaceFileTreePaths、目录缓存纯逻辑 workspaceFileTreeCache、虚拟滚动行构建纯逻辑 workspaceFileTreeRows，以及对应测试。

职责依赖树（当前文件仍平铺在同一目录；缩进表示调用/依赖关系）：

```text
WorkspaceFileTree.tsx
├── useWorkspaceFileTreeDirectories.ts
│   └── workspaceFileTreeCache.ts
├── workspaceFileTreeRows.ts
├── workspaceFileTreePaths.ts
└── WorkspaceFileTreeContextMenu.tsx
    └── useWorkspaceFileTreeContextMenu.ts
        └── useWorkspaceFileOperations.ts

*.test.ts(x)
└── workspaceFileTreeTestFixtures.ts
```

测试 fixture 只集中构造通用文件/目录节点；每个测试继续负责自己的 Gateway 响应、请求时序和交互状态。

文件分工：

- `WorkspaceFileTree.tsx`：文件树组件外壳与交互编排（展开/选择状态、渲染行、恢复与文件变更消费）；只调用下面的 hook 与纯逻辑模块，不内联路径算法、目录缓存规则或菜单动作。
- `WorkspaceFileTreeContextMenu.tsx`：右键菜单的渲染层。菜单目标来自 useWorkspaceFileTreeContextMenu，本文件只做标记与事件绑定。
- `useWorkspaceFileTreeContextMenu.ts`：菜单目标与位置、剪贴板条目、动作错误横幅，以及菜单交互（新建/粘贴/上传/复制/下载/系统显示/刷新/快捷路径族）的唯一所有者。四个集合写入口的持久请求与恢复委托给 `useWorkspaceFileOperations.ts`；任何菜单动作失败必须显式呈现，不得静默。
- `useWorkspaceFileOperations.ts`：已有新建、粘贴文本、复制及上传四入口的持久 intent、原请求 ID 查询、依赖与恢复编排；使用 `state/workspaceFiles/` 的单一 intent/Blob store，调用既有 API，不另存目录业务权威或定义文件执行规则。
- `useWorkspaceFileTreeDirectories.ts`：目录缓存与懒加载的唯一所有者（并发请求去重、分页合并、失败登记、LRU 保护集、精确子树失效 `invalidateDirectoriesUnder` 与基于它的全树重同步 `reloadExpandedDirectories`）。目录失效只有精确子树这一种语义，不存在第二套 stale 标记；全树重同步也必须复用同一原语。展开态与活动文件夹在外部，经 ref 传入以读取最新值。
- `workspaceFileTreePaths.ts`：已解析文件 URI 的父子关系、根内判断和剪贴板输入处理；根 URI 由固定目标后端提供，原生路径交目标机器解析，不在浏览器拼接真实路径。
- `workspaceFileTreeCache.ts`：目录缓存条目结构与唯一构造入口（加载中/成功/失败/过期四种形态）、LRU 淘汰与按层恢复的并发控制。任何目录条目字面量都必须复用这里的工厂函数。
- `workspaceFileTreeRows.ts`：唯一搜索匹配器和扁平行构建；组件的虚拟行和递归 DOM 共用同一个目录递归命中缓存。
- `workspaceFileTreeTestFixtures.ts`：仅供本目录测试复用稳定的文件/目录节点字段，测试专属响应、状态和请求行为仍留在各自用例中。

本目录与相邻子包的边界：

- `components/workspace/`：工作区会话级组件的平铺实现，本目录是文件树单一逻辑链路的下沉实现。
- `components/panels/`：可停靠面板容器；本目录只提供文件树内容，面板组合由 `components/workspace/WorkspaceAuxiliaryPanel` 承担。
- `components/overlays/`：通用浮层；本目录只消费 AnchoredOverlay。
- `state/display/`：展示态镜像；本目录不持有文件树业务权威状态，目录缓存只是渲染加速的派生数据。

# 可修改内容

- 文件树的选中、展开、拖放、剪贴板与虚拟滚动交互。
- 目录缓存与可见行构建的纯逻辑及其测试。

# 不可修改内容

- 不定义文件树协议类型权威（来自 `types/backend` 与 `api`）。
- 不在本目录实现后端文件操作规则、工作区路径安全校验或面板外壳布局。
- 不吞掉请求或交互错误；错误必须显式呈现。

# 规范

- 文件数据只以 `api` 返回和后端事件流为准，不在本目录伪造成功状态。
- 用户操作冻结目标、directory_key 与请求 ID；工作区切换、重连及响应丢失后沿原目标查询，owner 不匹配不能按 404 删除未确认 intent。目录缓存及 SSE 消费后端目录版本与 cursor，只收敛受影响目录。
- 目录、操作源及持久引用使用后端返回的 URI；原生路径输入在写入 intent 之前通过同一目标文件 owner 解析。
- 导入共享模块使用显式相对路径，禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行 `bun run --cwd src/clients/web build`。
