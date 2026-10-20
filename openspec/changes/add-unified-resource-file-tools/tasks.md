## 1. 公共合同与调用清点

- [ ] 1.1 等待并核验统一寻址1.4/3.4/7.1/第8组的根、分段编码、导航能力和产物单文件模板；消费者不自行定义kind/parser，公共合同完整后再推进依赖实现。
- [ ] 1.2 全仓清点文件工具、backend、UI/API/CLI、附件和工具输出生成/消费、Provider、历史投影、协议与测试，建立直接/间接caller清单与唯一文件owner边界。
- [ ] 1.3 固定工具目标/查询、limits/cursor、条件写入和逐文件执行结果类型，按spec区分寻址错误和文件执行错误；跨端操作授权独立于send/read/wait，不新增通用operation_id。
  - 2026-10-20 只读导航切片：公开 proto/DTO 增加 cursor、timeout_ms、scan_limit、max_response_bytes，以及 next_cursor、truncation_reason、view。Workspace/Gateway 共用现有 `ResourceNavigation`，文件执行错误独立于 grammar/resolver，并沿原 Gateway 来源及联邦路由传递；未增加逐文件身份或操作账本。写入前置、逐文件发布结果和模型工具调用合同仍未接通，本项保持未勾。
- [ ] 1.4 按公共 owner 的原生路径输入合同清点并迁齐相对/绝对/URI 目标解析，以及 `WorkspaceToolPathResolver`、filesystem tools、debugging、apply_patch、backend 输出转换和直接/间接测试；明确用户原生路径与 DeepAgents 虚拟路径的入口/类型，保持一条操作链，不扩大 patch 内部相对文件名的能力。
  - 2026-10-20 本地解析切片：删除 `_absolute_to_workspace_relative()` 及其去根回退；`ls/read_file/glob/grep/write_file/edit_file` 和源码调试共用显式工作区目标，接受相对路径、工作区内原生绝对路径和 Workspace files URI。原生解析复用 `core/path_utils.py::resolve_native_file_path`，URI 复用公共 grammar/文件 owner，身份只读不创建；不借中继进程 cwd。补丁内部文件名保持相对固定根，DeepAgents 虚拟根只在 backend 内部使用；调试 Skill 同步三种输入。尚未完成统一 URI 结果、其它资源根/远端与第5组产物调用迁移，本项保持未勾。
- [ ] 1.5 核验统一寻址 owner 2.7 的未注册目录 gateway/files 根及文件树浏览/编辑、快捷目录保存/重开、响应/持久化消费者已接通，本 change 复用该闭包接入模型文件工具；真实 UI 验收实际对象、编辑结果与工作区注册表不变，不要求或自动注册、不伪造 workspace_id、不收窄既有能力。

## 2. 本地技能包目录与轻量读取

- [ ] 2.1 接通ls/read_file/glob/grep到同一文件owner，先贯穿技能包根、SKILL.md、references/scripts多层导航；结果URI可继续访问，pattern不参与目标解析。
  - 2026-10-20 `ls` 垂直切片已完成：唯一 Container 导航 owner 经 runtime provider/factory/middleware 注入，同步及异步工具复用同一文件内核，删除旧 DeepAgents ls 调用。相对/原生绝对/URI 输入归同一目标；返回有界 URI JSON，可继续导航 references/scripts 多层目录，保留 cursor、扫描/响应字节限制及包含输入解析的总期限。普通 Gateway 导航使用独立请求，不创建来源观察 state；graph revision 提升为8并登记新的工具面 hash，旧 selector 明确拒绝。Skill 文件 read_file、glob/grep 与写入仍未迁齐，整项保持未勾。
  - 具名证据统一位于 `out/tests/temp/openspec_takeover/artifacts/runs/`：`resource-ls-unit-003` 162项通过（26.19秒外层），覆盖工具三种输入、不同relay cwd、一次编码、URI重开、cursor绑定、越界与装配；`resource-ls-graph-unit-004` 99项通过（12.58秒外层），补graph及装具回归。`resource-ls-http-final-004` 正式matrix为7 passed/132.08秒，含5项真实Agent工厂集成、真实Gateway轻量导航和模型实际ls三层目录，核目标owner PID、同一request_id回执、同名对照、来源/activation不增加及新assembly仍引用同一activation。三个运行均exit0且源码前后指纹一致。
  - 最后只修改 ls 的原生I/O及HTTP错误输出：保留执行错误域、type/errno，原始错误写入内部日志，模型回执不暴露宿主路径或endpoint；`resource-ls-error-boundary-005` 对真实工具入口补验45项通过。其余成功链路复用上述集成证据；不把这些局部通过称整组文件工具验收完成。
  - 失败归因保留：`resource-ls-unit-001/002` 中新测试分别误判未装配外部根与越界symlink错误，已按真实能力合同修正；压缩middleware denylist用例在完整 `git archive 3010c8ec` 基线 `resource-ls-baseline-001` 同样失败，保留原断言，最终定向运行具名排除此项。`resource-ls-http-001` 的观察装具只监听旧连接、漏记普通导航回执，已迁移；`resource-ls-http-002` 与 `resource-ls-model-http-003` 的新断言先后误把新Turn初始化及每次模型请求的assembly记录当作导航激活，最终在首次真实工具前取基线，并同时核来源/activation计数及assembly指向的snapshot/hash/provenance。原失败输出保留，未削弱目标、身份、正文或来源激活合同。
- [ ] 2.2 实现有界匹配/字节/deadline、截断及与目标查询绑定的分页；明确可变目录变化，不依赖逐文件持久快照。
  - 共享导航 owner 已实现目录/路径搜索的条目与序列化字节截断、总扫描量及单次 owner 期限；续页绑定真实 Gateway、规范根、操作、查询和全部限制。重新有界扫描时校验已消费前缀的 stat metadata，变更显式失败，结果披露 `view=live`；不把该摘要当内容 hash/revision，不建历史目录快照。UTF-8 读取只截完整字符，HTTP 层检查最终响应预算。完整模型 ls/read/glob/grep 与跨 Gateway 全程共享预算仍待迁齐，本项保持未勾。
  - TODO（续页性能）：当前每页重新执行有界扫描，长目录多页的重复工作待主体功能完成后实测优化；不得为此新增逐文件持久快照或第二目录事实源，不能把有界正确性验收称为性能达标。
  - 最终定向证据位于 `out/tests/temp/openspec_takeover/artifacts/runs/`：`resource-navigation-bounds-final-unit-004` 为118 passed/14.64秒，覆盖绑定 cursor、跨目录稀疏匹配续页、前缀变化、排序前扫描上限、UTF-8/JSON 字节预算、序列化共用期限、阻塞只读 I/O 的等待超时及错误回执身份；原生调用本身不会被异步取消强行中断，worker 恢复后仍检查原期限。`resource-navigation-bounds-final-http-003` 为3 passed/136.32秒，经真实 Gateway/Workspace、SSH 星型联邦及远端 Gateway Skill owner 验证257文件续页、最终 HTTP 字节限制、返回 URI 重开、文件执行错误透传、目标固定及 tracking/activation 不变。两项均exit 0、源码在运行中未变，不能替代模型 glob/grep 或跨端总预算验收。
  - 初轮完整组合 `resource-navigation-bounds-gateway-integration-001` 在已有失败后中断，为3 failed、2 passed/297.51秒、exit 2；原输出保留。文件执行错误透传及 UTF-8 旧断言已修齐并经最终定向运行验证；共享后端的既有路由歧义仍按3.3保留 TODO，未宣称该文件或整组全绿。另有 `resource-navigation-bounds-owner-routing-002` 的冷恢复 owner/request_id 定向证据，2 passed/121.44秒；后续导航期限变更由最终三场景补验，不以该旧运行代替新导航源码验收。
- [ ] 2.3 从真实工具入口验证中文/空格/点号/百分号文件名、一次解码及越界symlink拒绝；搜索大量文件前后身份/source/snapshot条目数不变。
- [ ] 2.4 与context第8组联验读取Skill无activation/tracking/source-control写入、显式skill_load冻结来源不变，旧挂载/白名单下线。

## 3. 目标端读取与搜索

- [ ] 3.1 复用现有Gateway联邦路由将文件调用交实际owner；不返回真实路径给source代执行，固定target与attempt channel lease分离。
- [ ] 3.2 两端同名不同内容的真实Gateway场景验证远端ls/read/grep及总deadline、不可达、不支持、撤权明确失败；source同名文件不变，无本地fallback。
- [ ] 3.3 验证调用期间切换激活工作区、重连和cursor续页不改变既定Gateway/workspace/root；跨界响应/记录不含locator或credential。
  - TODO（既有共享后端的路由消歧）：`test_shared_backend_routes_snapshot_navigation_and_concurrent_messages_to_true_owner` 中，同一后端同时由两个 Gateway 注册，固定工作区导航目前返回 `target_ambiguous`/409。原始失败保留在 `resource-navigation-bounds-gateway-integration-001`；与提交 `a6ce2bc9` 对照，该固定 Workspace 目标选择分支 AST 未变，证据为同任务 artifacts 下的 `resource-navigation-existing-route-ambiguity.json`，未重跑整套基线进程测试。源码保留同名 TODO，未改为任取一个目标或放宽测试，后续明确 owner 消歧后关闭本项。
- [ ] 3.4 从真实读写入口验证三种输入命中同一文件、相对路径不受 source/relay 当前目录影响、远端绝对路径按目标平台解释（含 Windows 盘符）；使用两端同名对照文件核实目标内容和 source/relay 零误写，目标失败不本地 fallback。

## 4. 工作区源码树与条件写入

- [ ] 4.1 接通files/root工作区根并平移现有write_file/edit_file/apply_patch到同一owner，绑定expected byte hash/owner revision或expected_absent；普通hash按需计算。
- [ ] 4.2 一次patch固定一个根，全部preflight后单文件原子发布；迁移既有文本新增/修改/删除/同根文件移动，source与destination都纳入能力及预期版本校验，拒绝跨根/绝对/越界路径、只读根/历史revision/领域对象。
- [ ] 4.3 验证并发编辑、expected_absent竞争、补丁preflight失败零写入，公布对服务内writer与外部writer不同的可验证CAS能力边界，不伪造跨文件事务。
- [ ] 4.4 故障注入覆盖多文件首个发布后失败、目标退出/响应丢失、取消尚未确认和重开核实；逐文件published/not_published/not_attempted/unknown准确，禁止自动重放/假回滚。
- [ ] 4.5 两个真实目标验证远端patch修改目标端、source不变；独立权限、失败能力、目标固定与UTF-8/raw-byte hash合同一致。
- [ ] 4.6 复用既有apply_patch add/delete与hints/move正式测试，保留输入、内容和changeset断言；覆盖删除版本冲突、移动目的版本/expected_absent冲突及目的已发布但源未删除的逐路径部分结果，不把文本文件操作扩成通用目录mutation。
- [ ] 4.7 在隔离目标中用外部绝对路径和工作区内同名对照文件验证真实读取、授权写入或拒绝零写入，确保工作区外目标不被重定向、不被强制先注册工作区；迁齐调用方后物理删除 `_absolute_to_workspace_relative()` 的 `path.lstrip("/")` 歧义回退，并将虚拟根旧测试迁入明确 backend 边界，保留终端/shell 原生路径与 Gateway 管理目录入口。
  - 2026-10-20 定向证据位于 `out/tests/temp/openspec_takeover/artifacts/runs/`：`file-target-final-regression-002` 129 passed、1 skipped/6.14秒，覆盖六个真实文件工具的三种输入、不同 relay cwd、外部实际文件及工作区内旧回退同名文件零误读/误写、调试入口、补丁与未注册目录 owner；Windows 原生主机用例在 Linux 上跳过，不能视为 Windows 验收。最终消除工具二次解析后，`file-target-codex-final-003` 对受影响入口补验28 passed/2.77秒；`file-target-backend-boundary-001` 6 passed/1.33秒核虚拟根仅属 backend；补齐旧 scheme 迁移 TODO 后的 `file-target-final-static-003` 对12个变更 Python 文件 Ruff exit 0。`file-target-final-openspec-001` 严格校验 exit 0。所有运行使用进程外 timeout/内存保护，保留命令、源码指纹、原始输出及退出码。
  - 扩展调试文件有13个旧失败，均发生于 canonical thread 绑定或 thread ID 校验；完整 `git archive dd4cd794` 基线运行 `file-target-debug-baseline-001` 同样13 failed、2 passed/1.47秒，本轮未放宽这些断言，也不声称整个调试套件通过。本轮未执行真实 Windows、完整 Gateway/远端及 Web 交互验收；当前模型工具明确拒绝外部路径，未注册目录 Web 访问能力沿既有 owner 保留，本项完整验收仍未关闭。

## 5. 会话产物与旧链路下线

- [ ] 5.1 平移附件和工具输出的新URI生成、store读取/授权、middleware/tool结果、用户消息、Provider以及Web/历史投影，保留原attachment/result业务ID和生命周期；附加文件URI不意味着可遍历Session私有目录。
- [ ] 5.2 验证新产物发布、owner重启、授权失败与删除结果；从真实read_file图片结果和Web original/thumbnail接口核验URI分派至原附件owner、Provider媒体投影不变，文本编码失败不误拒媒体；不新增通用二进制文件写入。
- [ ] 5.3 全仓检索旧scheme/旧路径解析的直接间接引用，全部迁移后删除boxteam-session解析分支和重复文件定位链；清开发中间数据，不迁旧数据、不留alias/双读。

## 6. 验证与交付

- [ ] 6.1 同步公开DTO/proto/OpenAPI/客户端和测试，静态分析及官方生成器检查；涉及Web则独立类型检查和bun build，正式测试按既有out/tests路径并带进程外保护。
  - 2026-10-20 ls 切片未改变公开HTTP/proto字段，无需重复生成。`resource-ls-final-static-002` 对26个变更Python文件 Ruff通过；`resource-ls-openspec-final-001` strict通过；辅助Browser服务最终 `resource-ls-browser-final-002` 为93个Node及195个Bun用例通过。同期清理装具的17项故障用例包含在 `resource-ls-unit-003`，HTTP页面装具及graph核验包含在 `resource-ls-graph-unit-004`；不运行延期的长性能场景。原始命令、指纹、退出码和日志见同任务artifacts/runs；这些局部证据不替代其余工具或完整Web验收，本项保持未勾。
  - 本轮导航协议切片通过官方 Buf format/lint/build、以真实提交 `a6ce2bc979477e03039c92f338d189158b8c1744` 为基线的 breaking 及官方生成器；OpenAPI 已导出。原件分别为 `resource-navigation-bounds-protocol-format-001`、`resource-navigation-bounds-protocol-generate-001`、`resource-navigation-bounds-openapi-001`，均exit 0。`resource-navigation-bounds-web-types-001` 真实 tsc 与 `resource-navigation-bounds-web-build-001` 正式 Web 构建均exit 0；之后未修改相应前端源码/生成类型。最终15个变更 Python 文件的 `resource-navigation-bounds-final-static-003` Ruff exit 0。所有测试均由 `run_task.py` 提供进程外 timeout/内存保护，保留命令、源码指纹、原始输出、实际退出码及进程回收记录；本项其余工具/写入/产物协议仍未完成，保持未勾。
- [ ] 6.2 完成首批真实验收：技能多层导航、搜索无逐文件注册、远端就地执行、激活切换目标固定、并发明确冲突、读Skill无激活；复用未变测试，对新差异及实际入口留固定候选证据。
- [ ] 6.3 固定Git树导出完整补丁、核全部调用迁移/删除和候选外改动，经独立审查及串行集成后勾对应任务；严格OpenSpec校验，不把规划完成当生产完成。
