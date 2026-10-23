# 目录用途

`src/server/` 实现 demo 后端：静态前端服务、txt 文件管理 API、attach 目标配置解析和 SSH 隧道代理。

```text
server/
├── config.js          解析目标配置、运行角色；相对配置和数据路径锚定 demo 根目录
├── fileStore.js       本机 txt 文件创建、串行保存和一致性快照
├── http.js            JSON 请求缓冲、受限请求体流、方法校验和 JSON 响应
├── server.js          角色入口、目标代理和服务关闭
├── sshCommand.js      SSH 参数组装及 Windows/POSIX known-hosts 空设备路径
├── staticFiles.js     浏览器资源路径校验与静态响应
├── targetFileClient.js 目标选择及本机/SSH 后端地址解析
├── upstreamProxy.js   20 MiB 限长请求流、固定及 Connection 点名头过滤、响应背压和下游断连取消
└── tunnel.js          SSH 子进程 stderr UTF-8 解码、端口分配、健康检查和清理
```

# 可修改内容

- HTTP API
- 文件存储逻辑
- SSH 隧道管理
- 配置解析与校验

# 不可修改内容

- 不要吞掉远程连接错误
- 不要返回虚假的默认文件内容

# 规范

- 端口默认 Gateway `7910`、前端 `7911`、后端 `7912`
- 相对目标配置、私钥和数据文件路径都相对 demo 根目录解析；外部位置必须传绝对路径
- 浏览器 API origin 的 Gateway 端口从 `/runtime-config` 读取，不在页面中固定默认端口
- 文件默认写入 demo 根目录下的 `.boxteam/`
- backend、gateway、frontend 并行监听；任一监听失败时等待其余结果并关闭所有已监听服务和 SSH 隧道
- 收到关闭信号时先等待 HTTP 服务启动收敛，再将隧道管理器置为终止态并拒绝新连接；每条 SSH 隧道先发 `SIGTERM` 并等待 1 秒，再发 `SIGKILL` 并等待 1 秒确认子进程关闭；关闭隧道后再等待 HTTP 请求退出，清理失败必须汇总报错
- SSH stderr 日志跨 Buffer chunk 解码时必须保留完整 UTF-8 字符；诊断尾部按原始字节数限制
- Gateway 以 Node Fetch async iterable 流式转发至多 20 MiB 的请求体，并按 Node 流背压转发上游响应；响应头发出后发生传输错误时关闭连接，不再写第二份 JSON 响应
- 代理请求和响应都过滤固定 hop-by-hop 头，以及 Connection/Proxy-Connection 值中点名的头；不得把单跳头转发到下一跳
- 浏览器断开代理响应时取消上游 fetch 并释放响应流；正常完成响应时不误取消
- 同一后端进程内对目标文件的快照和保存按调用顺序串行，避免并发 PUT 交叉写入或读取到半写内容；保存响应复用实际写出的 UTF-8 内容，只额外读取文件元数据，避免保存后重新读取整份文件
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
