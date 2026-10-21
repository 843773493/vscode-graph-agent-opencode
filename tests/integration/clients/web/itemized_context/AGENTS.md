# 目录用途

真实 Web → Gateway → Workspace → Saver 的冻结上下文检查 Integration。

## 可修改内容

- 本目录 Python fixture、Playwright 驱动与跨投影断言。

## 不可修改内容

- 不替代页面或后端上下文 API，不操作用户真实会话，不注册仓库根目录。

## 规范

- 普通上下文展示场景复用 model_stream 精确回放，不另开模型 HTTP 服务；HTTP 403 等网络错误专项可保留本地 HTTP 服务，不能标为严格 E2E。
- 查询展示可经真实 Saver 准备历史，不能据此宣称完整执行、fork 或恢复由产品入口通过。
- 工作区与产物镜像本测试 Python 文件路径；截图、下载和 trace 显式写入 artifacts。
- 启动前验证专用端口未占用，只清理本测试拥有的进程。
