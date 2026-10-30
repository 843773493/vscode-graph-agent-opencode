# 目录用途

验证 Session 子树删除、隔离恢复目录回收及 Session/Thread 创建的同步 I/O 结算边界。

# 可修改内容

- 原删除 journal、Session gate、保留期限和同步 I/O 的聚焦单元测试。
- 隔离预检、fence推进及真实SQLite锁竞争的原任务与恢复边界。
- Thread创建的原记录、holder、目录准备/核验/清理、发布确认及中止写入的结算边界。
- Session创建的冻结记录、目录准备、发布和隔离中止，保留各阶段原gate与同key串行规则。
- 复用既有 catalog/物理节点 fixture，受控暂停原生调用或线程池队列。

# 不可修改内容

- 不放 HTTP、Gateway、Web 或协作功能验收。
- 不以手填执行历史代替执行、fork 和恢复的产品入口。

# 规范

- 同步故障点必须自带释放上限，测试整体使用进程外超时与内存保护。
- 取消不得伪造原生终止；检查原 gate、locator 和 journal 的保持及显式重试。
