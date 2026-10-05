# 目录用途

工作区 Terminal 资源的唯一领域 owner，负责来源 Thread、保留范围、关联、版本、租约引用和恢复事实。

## 可修改内容

- 修改 Terminal 创建、查询、管理、Thread 卸载、Session 删除与孤儿回收。
- 修改 owner 持久记录和外部 PTY 事实核实。

## 不可修改内容

- 不实现 PTY、HTTP、WebSocket 或 Gateway 代理。
- 不创建第二套 operation lease 账本，不保留旧 Node 领域 owner 或迁移双轨。

## 规范

- 显式绑定工作区 UUID 与根目录；真实 SessionThread 必须经过 catalog 和生命周期准入。
- 外部结果未知时保留可发现记录并显式报错；停止/删除只在核实后结清。
- 持久化不包含绝对真实路径，客户端连接不隐式建立 Thread 关联。
