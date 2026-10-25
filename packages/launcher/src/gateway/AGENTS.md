# 目录用途

`packages/launcher/src/gateway/`管理Launcher持有的Gateway生命周期，不实现工作区Agent业务。

`supervisor.mjs`决定进程、generation和就绪状态；控制地址和消息传输归`control-channel.mjs`，公开流量的传输与目标切换归`public-listener.mjs`，监听器的事件等待和关闭由`server-lifetime.mjs`统一实现。

`supervisor.test.mjs`验证启动契约、信号和active恢复，`handoff.test.mjs`验证generation切换，`readiness.test.mjs`验证取消及就绪预算，`public-listener.test.mjs`验证真实HTTP流转发。测试中的进程替身不能替代真实Gateway启动；路径模拟不能替代Windows/macOS原生验证。

HTTP代理的客户端响应在未正常完成时关闭，必须释放上游请求，包含响应头尚未到达和流正文未结束两种情况；完整响应结束后允许复用上游连接。公开代理按target generation独立创建keep-alive Agent；切换target后关闭旧Agent的空闲连接，并禁止旧在途连接完成后重新入池，不能让新generation复用旧连接。监听只属于单次响应，不向keep-alive底层连接追加每次请求的监听器。客户端取消不代表后端业务已回滚或停止执行。断连测试按正式Launcher入口使用Node运行，只连接随机本地端口，不启动用户Gateway或工作区服务；Bun的HTTP shim未转发该关闭事件，不能代替实际运行时。

请求与响应流的上游错误由HTTP owner统一交付：响应开始前返回带原因的502，响应开始后销毁不完整响应，不拼入错误正文或伪造正常结束。目标断流不关闭公开监听器；测试应验证分块和定长正文中断，以及同一代理随后仍可读取健康目标。

WebSocket的`clientHead`表示客户端握手后已经收到的升级数据，`upstreamHead`表示目标101响应后已经收到的升级数据，两者分别写入对端Socket，不作为HTTP正文。客户端在101前发送FIN/end也须取消上游，不能仅等待完整close；目标拒绝升级时立即释放请求，不继续读取无界拒绝正文。升级后的正常流结束继续交给pipe，错误与客户端关闭由统一入口销毁持有的连接。

`public-listener-websocket.test.mjs`使用现有ws库和Node随机端口，验证真实首帧、双向消息、正常关闭码/原因，以及握手前后断开和拒绝后的资源释放；不运行用户服务，不把TCP断连表述为业务停止或回滚。

`requestGatewayHandoff`只交付一次结果并释放控制连接：完整响应前的EOF/关闭立即报告响应不完整，不继续等待总期限，也不把结果未知表述为未接管或已回滚。完整响应、拒绝、解析失败、连接错误和超时共用同一结束入口。

控制JSON按换行分帧；请求与响应的Socket统一设置UTF-8编码，由Node的流解码器保存跨数据块的残余字节，不对每块Buffer单独`toString`。路径、generation和错误正文里的中文或emoji不能因为传输分块而改变；成功、拒绝、EOF及超时仍走原有结束入口。

```text
out/tests/packages/launcher/src/gateway/control-channel-encoding/
├── workspace/<run_id>/<request|accepted|rejected>-<2|3|4>-byte-character/  本次Node的cwd
└── artifacts/<run_id>/
    ├── observer.mjs                         共用的独立Node观察入口
    └── <scenario>/                         固定input、分块字节及接收结果、raw和实际退出状态
```

编码fixture只把IPC端点映射到随机本地TCP端口，Socket、data事件、控制owner和`requestGatewayHandoff`均真实执行。第二块在第一块已进入接收端data事件后才写入，不以固定睡眠假定网络分块；2/3/4字节字符分别跨块测试请求、成功结果和错误原因，核完整字段值。运行和场景目录独占创建，Node临时目录显式指向自身cwd，正式现场与结果保留；fixture每阶段1秒、Node总计5秒，外层runner仍须保护。它证明Node流的编码边界，不代表原生Windows命名管道、Unix socket权限或真实Gateway启动验收。

`control-client.test.mjs`在独立Node进程把IPC端点选择替换成随机本地TCP端点，保留真实请求、响应及连接事件，验证上述生命周期；不创建用户socket或运行Gateway。它不验证Unix路径选择、Windows命名管道或真实generation切换，不能替代`handoff.test.mjs`中的完整handoff场景。

控制端点按规范化的home确定：POSIX保留短home中的`state/gateway-supervisor.sock`，长home使用同一home摘要对应的临时socket；备用地址仍超过100字节时在监听前明确报错。Windows使用`\\.\pipe\boxteam-gateway-supervisor-<home摘要>`命名管道，不对管道执行mkdir、chmod或rm。客户端和服务端调用同一地址解析器，不另存一份控制端点事实。

`handoff.test.mjs`承接完整控制结果、pending失败保留旧generation、成功切换并停止旧generation三场景。Node子进程的cwd为正式输出`out/tests/packages/launcher/src/gateway/handoff/workspace/<run_id>/`，临时目录显式为该cwd；因此深层输出根内的Unix socket可使用短相对地址。公开端口随机分配，以真实控制连接观察就绪，不用固定睡眠代替就绪。健康与generation读取经过真实HTTP和公开代理，Gateway进程仍为替身；该证据不代表真实Python Gateway启动或Windows/macOS原生通过。

`control-client.test.mjs`同样在自己的正式输出工作区运行Node并清理本次目录，传输仍为随机TCP替身；`control-channel.test.mjs`分别核路径规则，以及用独立Node文件/server替身证明Unix清理与命名管道不碰文件系统。

# 可修改内容

- Gateway进程监督、就绪观察、输出与信号转发、pending handoff和公开监听。
- 对应职责的启动器测试，以及共享的监听器生命周期操作。

# 不可修改内容

- 不实现Session、Job、资源URI等工作区业务规则，也不读写工作区业务数据。
- 不维护另一份配置事实，不把Gateway控制面移入工作区目录。

# 规范

- 使用ESM；职责间通过显式导入连接，迁移调用方后移除旧入口。
- HTTP和WebSocket共用公开监听器当前target；generation的创建和切换归supervisor所有。
- 保留原始异常和实际退出事实，测试使用进程外超时、内存保护和正式测试输出目录。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
