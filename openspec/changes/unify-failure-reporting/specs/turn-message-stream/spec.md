## ADDED Requirements

### Requirement: 实时失败与恢复结果共享原执行的失败事实

消息流终态失败、恢复快照及对应历史投影 SHALL 保留原 rollout convergence 终态提交中的同一失败原因和可用关联；失败不得只保存在进程内或仅写 Trace。原执行失败收尾 SHALL 先在原事务提交 rollout 终态、failure 和 history revision，再从已提交结果派生 stream.failed／snapshot 镜像，不以先广播再异步补存代替，不宣称两个存储跨文件原子。非终态 model.failed／工具错误仍走原 attempt 事件，不因此关闭成功 Turn。业务失败和消息传输失败 SHALL 保持区分，重连不能清除已提交失败或把它改写成连接错误。现有取消、超时、结果未知、可恢复性及执行顺序语义 MUST 保持。

#### Scenario: 失败提交后事件交付前断开
- **WHEN** 原执行 owner 已提交失败，客户端尚未收到终态事件就断开
- **THEN** 通过原回放或 snapshot 取得同一失败，刷新后的历史仍保留该原因，不触发调用重放或新的错误补偿任务

#### Scenario: 实时通道短暂断开但业务结果未失败
- **WHEN** 客户端传输连接断开，而原业务仍在运行或随后成功
- **THEN** 传输错误不被写成 Turn 业务失败；恢复后展示原权威业务结果

#### Scenario: 初始化阶段失败发生在首个 Item 之前
- **WHEN** 原执行边界收到初始化异常，Turn 已接受但尚无模型／工具事件
- **THEN** 原执行终态记录并发布实际原因，实时和历史均可取得，不要求先生成一个模型 Item 才能持久化失败

### Requirement: 跨存储中断沿已有启动恢复补齐终态镜像

现有 Job 启动恢复入口 SHALL 在决定恢复、标记丢失、收口工具和记录 Trace 前，以原精确 admission／execution 读取已提交终态。已提交成功、失败或取消／中断的执行 SHALL 沿原 owner 收尾队列／claim，按该结果补齐缺失的消息流终态，不重新恢复为活动 Job、不重放工具、不追加固定 execution_lost Trace。终态写入端口表示已处理的布尔结果 MUST NOT 被当成“本次确实写入了 execution_lost”。仅无已提交终态且原 checkpoint 恢复不成立时才走原丢失分支。Job 与消息流恢复 SHALL 复用同一原终态读取端口，不新增恢复缓存、协调服务或历史全扫描。

已有启动恢复 SHALL 对其原集合内未闭合流，按精确 SessionThread／Turn／execution 查询原 convergence 终态，优先用已提交结果补齐镜像，不把已知业务失败改成固定 execution_lost。恢复 SHALL 复用原 writer 及 execution／terminal commit 派生的稳定事件身份，同一提交重复恢复不得增加终态事件或重放业务执行；原 resumed job 跳过规则保持。没有已提交终态时 SHALL 沿原执行恢复／execution_lost 判定，不从 attempt、日志或当前配置补造原因。普通 snapshot／历史查询 SHALL 保持只读，不负责补写。

同一 terminal commit 的镜像内容冲突 SHALL 明确报告一致性错误。原 owner 合法产生 execution_lost 到 cancelled／interrupted 的后续修正时 SHALL 保留原提交和旧快照，经原 writer／控制事件路径交付当前结果，MUST NOT 当作同一提交损坏或用旧 event_id 写入不同内容。修正不得重复业务执行、首次完成通知或改变 first-end 分页／订阅顺序；启动恢复原集合内待交付的修正 SHALL 复用同一读取及幂等路径，不另扫全历史。系统 MUST NOT 为此新增 outbox、恢复队列、常驻协调服务或轮询。

#### Scenario: 已结束 Job 在消息流闭合前重启
- **WHEN** execution 的成功、失败或取消／中断终态已提交，Job 收尾或消息流尚未完成时进程重启
- **THEN** 真实启动恢复入口先消费该结果，队列／claim 正常收尾，Job 不恢复执行，Trace 不新增相矛盾的 execution_lost，消息流和历史保留该终态；成功结果不带 failure

#### Scenario: rollout 已提交但消息流尚未闭合
- **WHEN** rollout 失败终态提交后、消息流终态写入前进程中断，随后执行已有启动恢复
- **THEN** 未闭合流按原提交补齐同一失败原因及归属，恢复快照与历史一致，不用 execution_lost 替换已知原因

#### Scenario: rollout 提交前进程中断
- **WHEN** 异常已发生但 rollout 终态事务未提交，进程中断且原恢复无法恢复执行
- **THEN** 沿原规则判定 execution_lost，不把未持久异常或较早 attempt 失败伪造成已确认的业务失败

#### Scenario: 重复执行启动恢复
- **WHEN** 同一 execution 已按终态提交补齐镜像，再次运行已有启动恢复
- **THEN** 不新增终态事件，不重复业务执行，已闭合流保持原事件身份及原因

#### Scenario: 原 owner 确认后续控制修正
- **WHEN** 原 execution_lost 提交后，owner 按原规则提交 cancelled／interrupted 修正
- **THEN** 旧快照保留原结果，当前投影显示已确认修正，不报同一提交内容冲突、不重写旧事件，不重复执行或首次完成通知，原 first-end 游标顺序不变
