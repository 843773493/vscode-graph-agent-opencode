# 目录用途

存放 Browser 服务每个连接的传输合同与画面发送控制。

```text
transport/
├── protocol.js                客户端输入校验、服务端消息及二进制帧封包
├── protocol.test.js           线格式与输入拒绝合同
├── browserFrameFlow.js        单连接最新帧、ACK 和 socket 积压控制
└── browserFrameFlow.test.js   发送顺序、丢弃旧帧与定时器生命周期
```

页面在 `browserSession.js` 产生帧，后端入口将连接接入本目录；同一发布帧的封包按对象身份共享，ACK 和待发帧状态则逐连接隔离。

## 可修改内容

- WebSocket 输入校验、消息编码及二进制 JPEG 帧的线格式。
- 单连接画面流控、超时与对应单元测试。

## 不可修改内容

- 不在此管理页面、Chromium 运行时、工作区资源或 HTTP 路由。
- 不复制共享协议 schema；生成协议适配仍由 `browser/protocol/messages.js` 负责。
- 不保存共享页面的帧采样统计，或将某个连接的 ACK 状态放回页面。

## 规范

- 帧发布后不再修改；缓存只按帧对象身份复用，不维护第二份可变帧事实。
- 新帧先替换唯一待发帧，再由同一入口检查 ACK 和 socket 积压。
- 相对导入必须定位实际 owner；迁移后不保留旧路径转发或重复实现。
- 发送到 ACK 的计时与客户端 decode/draw 耗时分别解释，不把 ACK 当成像素已显示。
