## 1. 公共合同与调用清点

- [ ] 1.1 等待并核验统一寻址1.4/3.4/7.1/第8组的根、分段编码、导航能力和产物单文件模板；消费者不自行定义kind/parser，公共合同完整后再推进依赖实现。
- [ ] 1.2 全仓清点文件工具、backend、UI/API/CLI、附件和工具输出生成/消费、Provider、历史投影、协议与测试，建立直接/间接caller清单与唯一文件owner边界。
- [ ] 1.3 固定工具目标/查询、limits/cursor、条件写入和逐文件执行结果类型，按spec区分寻址错误和文件执行错误；跨端操作授权独立于send/read/wait，不新增通用operation_id。

## 2. 本地技能包目录与轻量读取

- [ ] 2.1 接通ls/read_file/glob/grep到同一文件owner，先贯穿技能包根、SKILL.md、references/scripts多层导航；结果URI可继续访问，pattern不参与目标解析。
- [ ] 2.2 实现有界匹配/字节/deadline、截断及与目标查询绑定的分页；明确可变目录变化，不依赖逐文件持久快照。
- [ ] 2.3 从真实工具入口验证中文/空格/点号/百分号文件名、一次解码及越界symlink拒绝；搜索大量文件前后身份/source/snapshot条目数不变。
- [ ] 2.4 与context第8组联验读取Skill无activation/tracking/source-control写入、显式skill_load冻结来源不变，旧挂载/白名单下线。

## 3. 目标端读取与搜索

- [ ] 3.1 复用现有Gateway联邦路由将文件调用交实际owner；不返回真实路径给source代执行，固定target与attempt channel lease分离。
- [ ] 3.2 两端同名不同内容的真实Gateway场景验证远端ls/read/grep及总deadline、不可达、不支持、撤权明确失败；source同名文件不变，无本地fallback。
- [ ] 3.3 验证调用期间切换激活工作区、重连和cursor续页不改变既定Gateway/workspace/root；跨界响应/记录不含locator或credential。

## 4. 工作区源码树与条件写入

- [ ] 4.1 接通files/root工作区根并平移现有write_file/edit_file/apply_patch到同一owner，绑定expected byte hash/owner revision或expected_absent；普通hash按需计算。
- [ ] 4.2 一次patch固定一个根，全部preflight后单文件原子发布；限定首批文本新增/修改，拒绝跨根/绝对/越界路径、只读根/历史revision/领域对象。
- [ ] 4.3 验证并发编辑、expected_absent竞争、补丁preflight失败零写入，公布对服务内writer与外部writer不同的可验证CAS能力边界，不伪造跨文件事务。
- [ ] 4.4 故障注入覆盖多文件首个发布后失败、目标退出/响应丢失、取消尚未确认和重开核实；逐文件published/not_published/not_attempted/unknown准确，禁止自动重放/假回滚。
- [ ] 4.5 两个真实目标验证远端patch修改目标端、source不变；独立权限、失败能力、目标固定与UTF-8/raw-byte hash合同一致。

## 5. 会话产物与旧链路下线

- [ ] 5.1 平移附件和工具输出的新URI生成、store读取/授权、middleware/tool结果、用户消息、Provider以及Web/历史投影，保留原attachment/result业务ID和生命周期；附加文件URI不意味着可遍历Session私有目录。
- [ ] 5.2 验证新产物发布、owner重启、授权失败与删除结果；附件原二进制接口保留其行为，不新增通用二进制文件写入。
- [ ] 5.3 全仓检索旧scheme/旧路径解析的直接间接引用，全部迁移后删除boxteam-session解析分支和重复文件定位链；清开发中间数据，不迁旧数据、不留alias/双读。

## 6. 验证与交付

- [ ] 6.1 同步公开DTO/proto/OpenAPI/客户端和测试，静态分析及官方生成器检查；涉及Web则独立类型检查和bun build，正式测试按既有out/tests路径并带进程外保护。
- [ ] 6.2 完成首批真实验收：技能多层导航、搜索无逐文件注册、远端就地执行、激活切换目标固定、并发明确冲突、读Skill无激活；复用未变测试，对新差异及实际入口留固定候选证据。
- [ ] 6.3 固定Git树导出完整补丁、核全部调用迁移/删除和候选外改动，经独立审查及串行集成后勾对应任务；严格OpenSpec校验，不把规划完成当生产完成。
