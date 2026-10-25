# 目录用途

提供 BrowserStateStore 与 BrowserManager 单测使用的最小完整工作区；只包含持久化 owner 所需的工作区身份文件，不包含无关项目源文件和多媒体资源。

## 可修改内容

- 可以维护本 fixture 的固定工作区身份元数据。

## 不可修改内容

- 不得在 fixture 中保存运行时生成的浏览器状态、检查点、截图或下载。
- 不得把测试运行时写入直接落到本目录；测试必须先复制到 `out/tests/`。

## 规范

- 此 fixture 仅适用于不读取项目源文件的 BrowserStateStore/BrowserManager 身份与持久化测试。
- 测试可以在复制后的工作区中覆写身份文件，不得修改源 fixture。
- 保留确定性内容，避免加入与状态存储无关的文件。
