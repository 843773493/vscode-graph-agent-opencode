# 目录用途

存放跨 Session 通信的编排接线：唯一 InboxAdmissionWorker 按持久状态索引恢复 target_accepted 未绑定 inbox，经现有 source+wakeup/admission owner 幂等建立 execution binding。

# 可修改内容

- 唯一 InboxAdmissionWorker 的异步消费、启动/关闭、claim 恢复与 binding CAS。
- 必填 binder typed port 与生产接线；通过唯一 RuntimeSourceDispatcher 委托 pending source+wakeup 和真实 Job/internal admission，不复制其准入实现。
- 冷读 binding lookup 与 durable observation 的编排；acceptance 事件和 backend startup 驱动同一 worker。
- observation/page 的冻结、同调用重试、ordinal 冲突、期限清理以及生产删除 drain/隔离原记录恢复接线；持久读写仍委托原 store owner，删除定位仍委托原 catalog journal。

# 不可修改内容

- 不扫目录、不依赖内存 future、不成为第二 ContextStore writer。
- 不直接读写 SQLite schema；持久 owner 是 SessionControlStore。
- 不直接追加 canonical item 或写 ContextStore；目标 execution 的准入只委托已有唯一 owner。
- read/wait 不创建或修补 inbox，不驱动 worker，不物化或唤醒目标 runtime。

# 规范

- 短 claim → 释放 Session gate → 委托 binding → 幂等 CAS；不得锁内重入准入 owner，也不得同时持有 source 与 target 两个 Session gate。
- claim 由 store 单事务闸门保证；跨库阶段按原 admission/preimage/wakeup identity 恢复，不声称分布式原子事务。失败经 record failure 保留 target_accepted 可恢复事实并显式上报。
- binder 输入只携带冻结 inbox 投影（含 admission_id/wakeup_key），不读磁盘或闭包补身份。
- 首次观察、重试、删除终结和期限清理使用同一冻结身份；过期明确返回原错误闭集，cursor 认证失败的 API/工具结果不披露内部解析细节。恢复不派生新观察或打开目标 runtime。
