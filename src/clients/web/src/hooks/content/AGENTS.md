# 目录用途

存放会话内容视图的装载与副作用编排 hook：useContentViewLoader 负责按当前内容视图装载会话变更、会话资源、Agent 状态快照与请求日志；useContentViewEffects 负责根据视图状态统一发起这些请求。

## 边界：contentViewLoaderTypes.ts 必须留在 hooks/ 顶层

`hooks/contentViewLoaderTypes.ts` 是留在 hooks/ 顶层的**全 hooks 层共享类型契约**（SetAppState / FinishWorkspaceRefresh / RefreshOptions），被约 22 处 import、横跨全部 hooks 子包引用。它**不是** content 组的私有实现，本子包只能单向 import 它（`../contentViewLoaderTypes`），**不得**把它搬进 hooks/content/ 或任何其它子包。移动它会强制 22 处引用反向依赖 content 子包，属于人为耦合与过度抽象。

# 可修改内容

- 会话内容视图的装载流程与各 loader 的编排顺序。
- 内容视图切换时的副作用触发与请求去重。
- 与上述逻辑直接相关的纯函数测试。

# 不可修改内容

- 不把 contentViewLoaderTypes.ts 移入本子包（它属于全 hooks 层共享契约）。
- 不定义后端协议类型的权威来源，不实现 UI JSX 组件。
- 不吞掉装载错误；错误必须写入状态或继续抛出。
- 不伪造内容视图状态，成功状态只来自后端返回值。

# 规范

- 装载与副作用编排保持输入输出显式，跨子包依赖使用 `../<子包>/...` 显式相对路径。
- 导入其他 hook 或共享模块时使用显式相对路径，禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行前端静态检查和 `bun run build`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
