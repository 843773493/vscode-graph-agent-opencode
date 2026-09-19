# 目录用途

存放跨 Session 通信的编排层 worker：InboxAdmissionWorker 按持久状态索引恢复 target_accepted 未绑定 inbox 并幂等建立 execution binding。

# 可修改内容

- InboxAdmissionWorker 的启动/关闭生命周期与单轮消费流程。
- binder 协议（typed port）与消费结果分类。

# 不可修改内容

- 不扫目录、不依赖内存 future、不成为第二 ContextStore writer。
- 不直接读写 SQLite schema；持久 owner 是 SessionControlStore。
- 不注入 canonical item、不唤醒目标 execution。

# 规范

- claim 由 store 单事务闸门保证；失败经 record failure 保留 target_accepted 可恢复事实并在轮末显式抛出。
- binder 输入只携带冻结 inbox 投影（含 admission_id/wakeup_key），不读磁盘或闭包补身份。
