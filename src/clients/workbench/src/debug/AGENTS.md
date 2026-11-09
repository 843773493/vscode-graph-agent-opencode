# 目录用途

将已发布的自有 Debug 声明和 Backend 唯一调试 owner 接入原 Code OSS Debug 服务、DAP 与界面。

# 可修改内容

冻结目标的启动动作、原 registry 的贡献投影、WebSocket adapter 和源码 URI 投影。

# 不可修改内容

不得创建第二配置、断点或运行状态 owner，不得伪造扩展描述、从焦点改投运行或直接访问插件端口。

# 规范

沿原 AbstractDebugAdapter 和 DebugService 生命周期；首次配置解析前冻结 Thread、插件绑定及版本。普通启动不隐式安装或开启插件，断开界面不代替明确终止。
