## 1. 公共合同与调用清点

- [ ] 1.1 等待并核验统一寻址1.4/3.4/7.1/第8组的根、分段编码、导航能力和产物单文件模板；消费者不自行定义kind/parser，公共合同完整后再推进依赖实现。
- [ ] 1.2 全仓清点文件工具、backend、UI/API/CLI、附件和工具输出生成/消费、Provider、历史投影、协议与测试，建立直接/间接caller清单与唯一文件owner边界。
- [ ] 1.3 固定工具目标/查询、limits/cursor、条件写入和逐文件执行结果类型，按spec区分寻址错误和文件执行错误；跨端操作授权独立于send/read/wait，不新增通用operation_id。
- [ ] 1.4 按公共 owner 的原生路径输入合同清点并迁齐相对/绝对/URI 目标解析，以及 `WorkspaceToolPathResolver`、filesystem tools、debugging、apply_patch、backend 输出转换和直接/间接测试；明确用户原生路径与 DeepAgents 虚拟路径的入口/类型，保持一条操作链，不扩大 patch 内部相对文件名的能力。
  - 2026-10-20 本地解析切片：删除 `_absolute_to_workspace_relative()` 及其去根回退；`ls/read_file/glob/grep/write_file/edit_file` 和源码调试共用显式工作区目标，接受相对路径、工作区内原生绝对路径和 Workspace files URI。原生解析复用 `core/path_utils.py::resolve_native_file_path`，URI 复用公共 grammar/文件 owner，身份只读不创建；不借中继进程 cwd。补丁内部文件名保持相对固定根，DeepAgents 虚拟根只在 backend 内部使用；调试 Skill 同步三种输入。尚未完成统一 URI 结果、其它资源根/远端与第5组产物调用迁移，本项保持未勾。
- [ ] 1.5 核验统一寻址 owner 2.7 的未注册目录 gateway/files 根及文件树浏览/编辑、快捷目录保存/重开、响应/持久化消费者已接通，本 change 复用该闭包接入模型文件工具；真实 UI 验收实际对象、编辑结果与工作区注册表不变，不要求或自动注册、不伪造 workspace_id、不收窄既有能力。

## 2. 本地技能包目录与轻量读取

- [ ] 2.1 接通ls/read_file/glob/grep到同一文件owner，先贯穿技能包根、SKILL.md、references/scripts多层导航；结果URI可继续访问，pattern不参与目标解析。
- [ ] 2.2 实现有界匹配/字节/deadline、截断及与目标查询绑定的分页；明确可变目录变化，不依赖逐文件持久快照。
- [ ] 2.3 从真实工具入口验证中文/空格/点号/百分号文件名、一次解码及越界symlink拒绝；搜索大量文件前后身份/source/snapshot条目数不变。
- [ ] 2.4 与context第8组联验读取Skill无activation/tracking/source-control写入、显式skill_load冻结来源不变，旧挂载/白名单下线。

## 3. 目标端读取与搜索

- [ ] 3.1 复用现有Gateway联邦路由将文件调用交实际owner；不返回真实路径给source代执行，固定target与attempt channel lease分离。
- [ ] 3.2 两端同名不同内容的真实Gateway场景验证远端ls/read/grep及总deadline、不可达、不支持、撤权明确失败；source同名文件不变，无本地fallback。
- [ ] 3.3 验证调用期间切换激活工作区、重连和cursor续页不改变既定Gateway/workspace/root；跨界响应/记录不含locator或credential。
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
- [ ] 6.2 完成首批真实验收：技能多层导航、搜索无逐文件注册、远端就地执行、激活切换目标固定、并发明确冲突、读Skill无激活；复用未变测试，对新差异及实际入口留固定候选证据。
- [ ] 6.3 固定Git树导出完整补丁、核全部调用迁移/删除和候选外改动，经独立审查及串行集成后勾对应任务；严格OpenSpec校验，不把规划完成当生产完成。
