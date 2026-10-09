# 目录用途

编排同一Session的团队fanout投递、恢复和team_state资源发布。`fanout_service.py`调度既有Session control ledger与各精确Thread的pending source owner；本目录不保存第二份协作事实。

# 可修改内容

- 按冻结recipient集合逐项投递、幂等恢复与Registry不可变内存snapshot发布。
- generation LifetimeScope内的订阅释放，以及原业务显式task wakeup的消费。
- 与原activation boundary、删除drain和启动恢复的调用接线。

# 不可修改内容

- 不另建board、事件、fanout或recipient数据库/JSON日志；权威记录归SessionControlStore。
- 不直接写canonical item或代替CSM/ContextSourceControlStorage事务，不为fanout物化cold runtime。
- 不在request capture读取ledger、源文件或重建当前membership，不实现跨Session团队兼容。

# 规范

- ledger mutation与fanout记录同事务冻结generation、revision、event hash和recipient集合；提交后不按当前成员重算收件人。
- fence关闭后的原member取消只结清既有ledger记录，由删除drain终结原fanout；不申请新lease、创建新fanout或写pending source。
- 各recipient只通过其原owner提交pending observation；重启仅补未完成者，已提交revision不重写。
- 默认turn在active slot冻结team_state且整个tool-loop不漂移；显式model_call仅在安全call边界消费Registry最新已发布snapshot。
- 启动恢复与required snapshot就绪先于dispatch；fence关闭后的未投递者明确terminal，不晚到append。
- 原operation lease、holder token和Session删除归既有生命周期owner；本目录只协调，不建立第二状态机。
- 异常保真实原因；测试覆盖真实投递/恢复闭包并使用进程外保护，不能用内存计数冒充持久结果。
