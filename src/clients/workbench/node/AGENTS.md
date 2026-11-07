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
- 制品下载只在原 RequestService 的五个 VSIX／签名 callSite 接缝改由目标 Python；原 Gallery、DownloadService、签名校验及安装计划保持原服务。具名 loopback 回调能力和原 request_id 只绑定本次排队安装的 AsyncLocalStorage，不持久化、不跨请求复用；真正下载缺少传输必须明确失败，本地文件安装仍可用。回调 POST 不重放、不重定向，原代理配置及 shell 环境优先级必须保留，本机认证与回调错误头不得送原站。制品请求沿原 `logUrl:false` 日志合同只写 host、状态及错误类别，不写签名 URL、请求／响应头或异常正文。
- 服务内串行处理安装及精确发布。current-only 发布响应流持有准入到 Python 实际 I/O 结清；Web 准入在同次控制操作内复用原发布函数核精确集合并返回冻结描述，不能持队列等待整个 Web session。错误返回原归属并允许后续合法请求；停止准入后排空原异步回调及缓存写入，释放消费者，沿原watch([])停止provider watcher并释放provider，返回drained后自然退出。Gateway换代只释放句柄，完整停机才调用shutdown并核实际进程；不得用process.exit或每请求dispose替代结算。
- 当前库存明确限于目标默认 profile 的用户安装项；不将它声称为系统扩展、开发扩展或完整窗口启用视图。
- VSIX 安装结果与完整库存由同一次串行 Node 控制操作生成；Python 不在释放安装请求后另读库存拼回执。保留原安装器的现有版本复用，物理卸载和运行版本切换须沿各自生命周期完成。
- 市场安装由目标原 Gallery 按 ID／版本重新取对象并交原 installFromGallery；元数据写入只接受公开布尔选项，UUID、publisher 和平台等事实从原 Gallery 重取。选项校验与字段映射共用生成 proto descriptor，不复制逐字段转换。库存中的平台、metadata 和 README／CHANGELOG 相对路径消费同次原安装／扫描服务，不按浏览器平台或候选正文猜测。
- `installed.reused`消费同次原安装器context的实际版本复用结果；不能以候选manifest声称已替换正文。私有进程回执保留原ExtensionManagementError.code，文件系统错误继续使用原Provider错误映射。
- 精确发布描述沿原scanner核ExtensionKey、逻辑ID和当前profile资格；不读取包内路径或正文。Web 旧实例资格与描述由原 Backend session 保留，包边界和字节统一由 Python 的 publication_file 检查；current-only 仍即时核当前成员。TODO(4.4)：Node 真实运行集合与最终引用回收未接通，不能启用全局物理 cleanUp。
- Web 准入在本次串行请求内共用原 profile 扫描对象生成库存与发布描述，不逐包重复扫描 profile／manifest；后续请求仍重新扫描，不能持久缓存准入资格或省略原件、精确版本和发布根校验。
