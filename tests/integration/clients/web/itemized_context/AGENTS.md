# 目录用途

真实 Web → Gateway → Workspace → Saver 的冻结上下文检查 Integration。

## 可修改内容

- 本目录 Python fixture、Playwright 驱动与跨投影断言。

## 不可修改内容

- 不替代页面或后端上下文 API，不操作用户真实会话，不注册仓库根目录。

## 规范

- 唯一替身为本地 Provider HTTP 协议桩，不能标为 E2E。
- 工作区与产物镜像本测试 Python 文件路径；截图、下载和 trace 显式写入 artifacts。
- 启动前验证专用端口未占用，只清理本测试拥有的进程。
