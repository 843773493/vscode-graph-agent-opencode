# 目录用途

目标 Backend 持有的 Code OSS Node 基础设施组件，不属于浏览器 renderer 或 Electron main。

# 可修改内容

- 原生扩展管理、profile 和扫描服务的装配与私有进程入口。
- `extensionConfiguration.ts`复用管理组件已登记的原FileService／DiskFileSystemProvider，提供默认profile settings与稳定多根view文件；原生位置由原profile／environment服务取得，不推导路径或维护第二保存基线。

# 不可修改内容

- 不复制扩展安装、解包、manifest 校验或 profile 持久化实现，不新增安装账本。
- 不从路径、主机名或 remoteAuthority 推导 Backend 身份。

# 规范

- 与宿主共用固定上游、专用 Node 和冻结依赖。安装与库存必须消费同一组原版服务实例。
- 管理控制只接受父进程的标准输入，标准输出只返回控制结果，日志写标准错误；进程由 Backend 原子进程 owner 回收。
- 每个管理进程只接收一条业务请求；ready等待原watcher、目录事件和下载缓存初始化，drained等待原异步回调及缓存写入结束。消费者先释放，再沿原watch([])停止provider watcher，最后释放provider并自然退出；不得用process.exit或常驻内存profile替代结算。
- 当前库存明确限于目标默认 profile 的用户安装项；不将它声称为系统扩展、开发扩展或完整窗口启用视图。
- VSIX 安装结果与完整库存由同一次串行 Node 控制操作生成；Python 不在释放安装请求后另读库存拼回执。保留原安装器的现有版本复用，物理卸载和运行版本切换须沿各自生命周期完成。
- 配置控制仅接受已登记的两个文件及直接父级stat；多根view保留原生folders、settings与注释，单根不物化workspace文件。浏览器具名URI、认证generation／view和共享watcher由既有Python owner负责，不将本组件局部验证宣称为完整窗口隔离已验收。
- 配置操作从原session冻结数据登记Map；registerView不读写磁盘，只有初始化的prepareView可以物化workspace文件。文件删除后stat/read保留原FileNotFound，不得隐式恢复。
