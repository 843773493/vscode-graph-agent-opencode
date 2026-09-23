# 目录用途

存放应用根级（AppProvider）链路：AppContext / ComposerContext 契约与消费者、启动种子状态、AppProvider 各条装配链路（布局设置同步、会话目录动作、预览标签投影、默认视图提示、会话资源轮询）。

与相邻目录的分工边界：

- `hooks/*/`（session、workspace、panel、composer 等）：面向单一业务域的 hook；本目录只做根级装配与上下文契约。
- `state/`：纯函数状态变换的权威来源；本目录复用它们，不重复实现。
- `components/shell/`：应用外壳组件；本目录只提供状态与动作，不渲染 JSX 组件。

# 可修改内容

- AppContext / ComposerContext 的类型契约、Context 对象与消费者 hook。
- AppProvider 的启动种子状态与各条根级装配链路。
- 与上述逻辑直接相关的纯函数测试与链路测试。

# 不可修改内容

- 不定义后端协议类型的权威来源，不复制业务状态权威。
- 不吞掉错误；错误必须写入状态或继续抛出。
- 不在本目录实现 UI JSX 组件，也不渲染任何业务视图。

# 规范

- 新增链路保持同名导出，只挪实现，禁止引入兼容适配层。
- 导入其他模块使用显式相对路径，禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行前端静态检查和 `bun run --cwd src/clients/web build`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
