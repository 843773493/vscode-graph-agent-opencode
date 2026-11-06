# 目录用途

验证固定上游 Node Extension Host 的构建、Backend 进程基础设施、原 IPC／RPC、真实扩展激活及目标文件访问。

# 可修改内容

- 明确主线程替身边界的宿主协议测试、进程生命周期测试和测试扩展。

# 不可修改内容

- 不实现产品宿主管理、安装账本或另一套扩展协议；不运行上游测试。

# 规范

- 使用显式 Node runtime 和隔离依赖，产物按测试路径写入 out/tests；所有运行带进程外超时和内存保护。
- 原型不代表 Gateway、目标授权、多目标窗口 registry 或完整产品验收。
- `test_node_host.py`使用真实Backend registry与原BackendLifespan关闭宿主；ipc直接接owner，gateway/relay使用真实Uvicorn与产品认证路由，TCP listener只桥接原协议字节。Uvicorn关闭完整lifespan装配，未装配工作区业务图和真实SSH；窗口主线程仍是后续验收边界。
- `extension_state.test.mjs` 编译真实描述类、接收端、Registry、Sequencer、原 Manager 和生命周期 owner；只在文件 realpath、进程／RPC 传输和主线程服务边界提供明确装具。候选发布、失败代际移除和 eager 并发必须经过这些原方法，不能复制状态机；该组合验证不等同 Gateway 授权或完整多目标产品验收。
- 内部回调代际验证使用原owner生成的API及原eager／lazy Manager关闭入口；覆盖断连等待／失败、直接dispose、迟到激活和同位置替换，不能用测试自行设置active变量代替产品准入。
- 查询／激活验证运行原MainThread、owner、Manager和ExtHost activator；仅窗口TimerService的DI模块为具名替身，不能据此宣称验证窗口性能服务或完整视图投影。保留`api:none`依赖成功／失败与原错误归属，核非所属状态回调拒绝、唯一持有者派发和歧义零调用；不能只检查改写后的源码文本。
- `node_host.mjs` 通过真实原 RPC 增删、替换同 ID 的未激活扩展，并在合法版本激活真实代码；回调观察真实 `extensions.onDidChange`，核旧／重复版本与非法候选拒绝及另一宿主不变，不据描述替换宣称支持已激活扩展动态卸载。
