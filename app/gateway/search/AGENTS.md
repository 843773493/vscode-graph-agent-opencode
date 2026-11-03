# 目录用途

Gateway 联邦搜索的范围解析与跨目标查询编排；领域匹配仍由工作区 Search Provider 的原 owner 执行。

## 可修改内容

- 稳定 owner、显式范围、冻结集合及原 owner 独立续查的协调。
- 搜索请求的有界调度、取消、进度和最终完成合同。

## 不可修改内容

- 不读取工作区 `.boxteam/`，不复制领域匹配器或保存第二套业务正文。
- 不依赖浏览器活动态，不因查询启动、连接或激活目标。

## 规范

- `scope.py` 和 `scheduler.py` 的核心不依赖 HTTP、SSE 或 UI；`transport.py` 只沿原登记路由读取有界领域页，SSE 编码归 `routes/`。
- 目标用稳定 Gateway／workspace 身份，执行前重解当前登记路由；失效分支不得换投其它 owner。
- 范围和原 cursor 可由调用方传回，不能引入持久搜索 session 或全域 revision。
