# 目录用途

存放仓库正式测试、跨客户端场景、运行基础设施与测试套件注册。unit、contracts、integration 和 e2e 按验证目标归属；目录层级不代表外部依赖的真实性，也不是证据强度排名。

```text
tests/
├── conftest.py   共享配置、工作区路径与进程生命周期 fixture
├── unit/          单模块行为；业务测试镜像被测模块，sdk/ 验共享 SDK，harness/ 验测试设施
├── contracts/     公开协议、schema 与跨语言契约
├── integration/   多模块或真实进程组合，允许明确的外部服务替身
│   └── harness/   测试运行设施自身的真实子进程与传输行为
├── e2e/           用户明确提出的完整业务流程验收，模型模式可选
├── clients/       由客户端源码合同测试共享的稳定语义选择器
├── fixtures/      只读输入模板；由测试复制或读取，不是运行工作区
│   ├── workspaces/    完整工作区模板
│   ├── model_stream/  上游协议帧、录制资产与场景预期
│   └── itemized/      跨语言文本与 hash 测试向量
├── harness/       Python/JS 运行上下文及客户端辅助服务进程装具
├── support/       多个测试复用的端口、工作区准备、协议及断言辅助代码
└── runner/        suite 注册、选择、资源保护和命令执行
```

先按测试需要证明的边界选择层级，再在层级内按业务职责组织。共享 SDK 测试归 unit/sdk、contracts/sdk 和 integration/sdk，内部再按 Python／Node 分组；产品及具体插件的接入验收仍归各自 owner，不恢复顶层 tests/sdk。仅服务于一组测试的 helper 留在该组目录；跨组复用后再提升到 `support/` 或对应运行设施目录，不复制一份到每个层级。静态输入归 `fixtures/`，可变工作区和执行产物归 `out/tests/`，不要由数据文件名决定它的归属。

## 可修改内容

- 测试用例、fixture、驱动、场景、选择器、运行上下文和测试矩阵。

## 不可修改内容

- 不要在测试目录实现生产业务逻辑。
- 不要把回放结果表述为真实 Provider 已验证，也不要在前置条件缺失时自动切换模型模式。

## 规范

- 默认先在共享根 worktree 验证本次完整改动，必要时同步到固定 dev，前两者不适合才使用实验 worktree；用户指定目录时显式绑定。并发修改相关源码后核对结果对应的实际输入，不能将旧结果归给新工作树。
- worktree 选择不改变业务工作区隔离或下述集成闸门；不得为并行绕过锁或抢占另一任务的服务。迁移测试使用经核实的 release 旧契约输入与隔离副本，不能以临时开发 Home 推导生产兼容需求，也不访问真实用户 Home。
- 根 `conftest.py` 自动加载集成/E2E session 闸门；同一 Git 仓库的 worktree 共用主工作树 `out/tests/harness/python/integration_gate/runtime/session.lock`，一次只运行一个 pytest 集成/E2E进程，xdist worker 也各自占名额。无 `.git` 的源码归档使用显式项目根。名额在 session 装具清理后释放；等待最多600秒，矩阵外部期限仍包括排队时间。锁文件是稳定控制位置，不能删除或按测试产物清理。单测、非pytest命令、不同clone和旧源码不会自动受此闸门限制，外部超时与内存保护仍须保留。
- Integration 验证生产组件之间的协作、协议和故障边界；可以使用真实依赖或明确的外部替身，不以有无模型回放决定归属。
- 新增 E2E 来自用户主动说明的完整业务流程；在用例说明中写明入口、关键步骤和可观察的最终结果即可，不另建审批或来源登记系统。客户端流程经过实际客户端、Gateway 和 Workspace；后端系统流程按声明的公开入口验收，不冒充客户端覆盖。
- E2E 可使用真实模型、上游录制重放或手工 SSE；共用现有 transport、fixture 和驱动，不复制场景主体或另起模型服务。同一文件切换模型参数不改变目录及 pytest 层级标记，产物注明实际模式、响应资产来源及被替换边界；实时录制仍属于真实调用，重放和手工 SSE 不证明真实 Provider 可用。
- 现有归属不明确的测试暂留原目录，后续触及时再按验证目标判断，不因新语义批量迁移或复制；unit、contracts 和原套件入口保持现有归属。
- 正式产物路径镜像测试文件路径写入 `out/tests/`；临时诊断写入 `out/tests/temp/YYYY/MM/DD/<task_name>/`。
- 性能排查先使用现有矩阵和默认收据，不临时复制观察器；核心排查命令、报告查询与比较见 `tests/harness/performance/AGENTS.md`。SQL/内部调用次数是诊断数据，不自动变成业务断言。
- 正式测试内部的临时目录和 `BOXTEAM_HOME` 也必须位于对应 `out/tests/.../runtime/`，并按运行与节点隔离。
- pytest 自动按文件保留最近 2 次已结束运行，额外保留最近 1 次失败运行（去重），参数变体整组保留；成功 runtime 关闭后回收，失败保存已关闭资源的工作区现场，共享装具延后到作用域结束。`preserve_test_state` 显式保留，异常退出及清理失败的现场禁止自动覆盖或删除。共享证据跟随最后一个消费者回收，详见 `harness/python/AGENTS.md`；独立脚本和矩阵外层产物不在此清理范围内。
- 测试层标记与 integration／e2e 文件分组由根 `conftest.py` 按实际文件归属统一生成；子目录 collection hook 不得标记全 session。runtime manifest 由每个 pytest session 独占，不共用可重写文件。
- 当前客户端 E2E 目录主要覆盖 Web；Linux Electron 的 ASAR 与模型回放测试保留在 `integration/clients/electron/`，不因模型模式自动迁移。Web parity、模型回放及任一平台结果均不能冒充未实际运行的原生平台或真实 Provider 证据。
