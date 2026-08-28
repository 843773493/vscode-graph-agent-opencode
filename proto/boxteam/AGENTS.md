# 目录用途

`proto/boxteam/` 存放 BoxTeam 的版本化公开协议域。

## 可修改内容

- 可以维护 `common`、`gateway`、`workspace`、`terminal` 和 `browser` 协议子目录。

## 不可修改内容

- 不要跨域复制业务消息来绕过 import。
- 不要把 Gateway 作为其它服务业务协议的拥有者。

## 规范

- 每个协议域必须按 `v1`、`v2` 等版本目录隔离。
- 跨域引用必须使用显式 `.proto` import。
