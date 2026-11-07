# 目录用途

目标 Gateway 托管的 Code OSS Node 扩展管理基础设施组件，不属于浏览器 renderer、Electron main 或 Backend 运行宿主。

# 可修改内容

- 原生扩展管理、profile 和扫描服务的装配与私有进程入口。
- 只提供安装、库存及精确发布／profile位置描述；配置和包文件字节由目标Python处理，不能恢复通用Node文件操作或视图物化接口。

# 不可修改内容

- 不复制扩展安装、解包、manifest 校验或 profile 持久化实现，不新增安装账本。
- 不从路径、主机名或 remoteAuthority 推导 Backend 身份。

# 规范

- 与宿主共用固定上游、专用 Node 和冻结依赖。安装与库存必须消费同一组原版服务实例。
- 管理组件直接提供loopback HTTP具名接口，沿原本机token认证且拒绝浏览器来源；自己持有原OS安装域独占锁。Gateway核验发现记录、运行身份及实际日志后接管，不抢锁或另起代理进程。标准输出和错误只进原日志体系，不保留stdio管理协议。
- 服务内串行处理安装及精确发布读取，发布响应流持续持有准入直到Python实际I/O结清。错误返回原归属并允许后续合法请求；停止准入后排空原异步回调及缓存写入，释放消费者，沿原watch([])停止provider watcher并释放provider，返回drained后自然退出。Gateway换代只释放句柄，完整停机才调用shutdown并核实际进程；不得用process.exit或每请求dispose替代结算。
- 当前库存明确限于目标默认 profile 的用户安装项；不将它声称为系统扩展、开发扩展或完整窗口启用视图。
- VSIX 安装结果与完整库存由同一次串行 Node 控制操作生成；Python 不在释放安装请求后另读库存拼回执。保留原安装器的现有版本复用，物理卸载和运行版本切换须沿各自生命周期完成。
- `installed.reused`消费同次原安装器context的实际版本复用结果；不能以候选manifest声称已替换正文。私有进程回执保留原ExtensionManagementError.code，文件系统错误继续使用原Provider错误映射。
- 精确发布描述沿原scanner核ExtensionKey、逻辑ID和当前profile资格；不读取包内路径或正文。旧运行实例的版本资格、包边界和读字节分别由原Backend运行记录与Gateway Python执行。
