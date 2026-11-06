# 目录用途

目标 Backend 持有的 Code OSS Node 基础设施组件，不属于浏览器 renderer 或 Electron main。

# 可修改内容

- 原生扩展管理、profile 和扫描服务的装配与私有进程入口。

# 不可修改内容

- 不复制扩展安装、解包、manifest 校验或 profile 持久化实现，不新增安装账本。
- 不从路径、主机名或 remoteAuthority 推导 Backend 身份。

# 规范

- 与宿主共用固定上游、专用 Node 和冻结依赖。安装与库存必须消费同一组原版服务实例。
- 管理控制只接受父进程的标准输入，标准输出只返回控制结果，日志写标准错误；进程由 Backend 原子进程 owner 回收。
- 当前库存明确限于目标默认 profile 的用户安装项；不将它声称为系统扩展、开发扩展或完整窗口启用视图。
