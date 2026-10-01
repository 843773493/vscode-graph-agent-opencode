# 目录用途

`components/workspace/fileTree/` 存放工作区文件树族的实现：主组件 WorkspaceFileTree、右键菜单渲染 WorkspaceFileTreeContextMenu、右键菜单动作 hook useWorkspaceFileTreeContextMenu、目录缓存与懒加载 hook useWorkspaceFileTreeDirectories、路径语义纯逻辑 workspaceFileTreePaths、目录缓存纯逻辑 workspaceFileTreeCache、虚拟滚动行构建纯逻辑 workspaceFileTreeRows，以及对应测试。

文件分工：

- `WorkspaceFileTree.tsx`：文件树组件外壳与交互编排（展开/选择状态、渲染行、恢复与文件变更消费）；只调用下面的 hook 与纯逻辑模块，不内联路径算法、目录缓存规则或菜单动作。
- `WorkspaceFileTreeContextMenu.tsx`：右键菜单的渲染层。菜单目标来自 useWorkspaceFileTreeContextMenu，本文件只做标记与事件绑定。
- `useWorkspaceFileTreeContextMenu.ts`：菜单目标与位置、剪贴板条目、动作错误横幅，以及全部菜单动作（新建/粘贴/上传/复制/下载/系统显示/刷新/快捷路径族）的唯一所有者。任何菜单动作失败必须显式呈现，不得静默。
- `useWorkspaceFileTreeDirectories.ts`：目录缓存与懒加载的唯一所有者（并发请求去重、分页合并、失败登记、LRU 保护集、展开目录刷新）。展开态与活动文件夹在外部，经 ref 传入以读取最新值。
- `workspaceFileTreePaths.ts`：文件树路径的唯一语义权威（根路径常量、父子推导、作用域内判断、变更路径归一、绝对路径拼接、剪贴板路径解析）。任何路径归一都必须复用这里的实现。
- `workspaceFileTreeCache.ts`：目录缓存条目结构与唯一构造入口（加载中/成功/失败/过期四种形态）、LRU 淘汰与按层恢复的并发控制。任何目录条目字面量都必须复用这里的工厂函数。
- `workspaceFileTreeRows.ts`：把目录缓存与展开状态编译成可渲染的扁平行。

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
- 导入共享模块使用显式相对路径，禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行 `bun run --cwd src/clients/web build`。
