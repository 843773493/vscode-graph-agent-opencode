---
name: browser-control
description: 当用户要求列出、打开或操控浏览器页面、查询当前后台浏览器网站，或要求调用 clickElement、dragElement、handleDialog、hoverElement、listBrowserPage、navigatePage、openBrowserPage、readPage、runPlaywrightCode、screenshotPage、typeInPage 这些浏览器扩展工具时，读取本 skill。
allowed-tools: listBrowserPage, openBrowserPage, readPage, screenshotPage, navigatePage, clickElement, typeInPage, hoverElement, dragElement, handleDialog, runPlaywrightCode
---

# browser 控制扩展工具

## 总规则

这些浏览器工具不会直接出现在模型 tools 列表中。必须发起真实工具调用，使用固定入口 `invoke_custom_tool`。

所有调用都采用下面格式：

```json
{
  "tool_name": "目标浏览器工具名",
  "arguments": {}
}
```

工具返回中的 `pageId` 是可附加浏览器实例 ID。一个实例可以包含多个标签页；`readPage` 返回 `active_page_id` 和 `pages`，其它页面工具默认操作当前活动标签页。资源视图会显示该浏览器资源，用户可以从资源卡片打开并默认 attach。

用户和模型默认可以共享操作同一浏览器。所有操作都进入统一队列，结果中的 `operation_revision` 是已完成操作的单调版本号。如果工具返回“用户锁定了浏览器”，不要重试或绕过锁；告诉用户当前不能操作，等待用户解锁后再继续。

如果任意浏览器工具返回的页面状态中包含 `pending_dialog` 或 `pending_file_chooser`，必须先调用 `handleDialog` 处理该对话框，再继续后续页面操作。

当只是为了验证工具链或构造临时测试页面时，优先把完整 HTML 编码成 `data:text/html;charset=utf-8,...` 传给 `openBrowserPage`。不要假设 `http://127.0.0.1:<临时端口>/...` 对浏览器管理器进程可达；只有用户明确说明该服务已经在浏览器管理器所在机器上启动时，才使用这类本地端口 URL。

当用户询问当前 Session 已有哪些浏览器、正在浏览什么网站或需要先获得 `pageId` 时，必须先调用只读的 `listBrowserPage`。不要使用 `grep`、`read_file` 或其它文件系统工具读取 `.boxteam/browser-manager/browsers.json`；该状态文件不是模型工具接口，可能包含已删除记录、过期状态和内部字段。

## 工具 schema

### listBrowserPage

只读列出当前 Session 中未删除的浏览器页面。该工具不需要参数，不会创建、唤醒、导航或修改页面；返回的 `pages[].pageId` 可继续传给 `readPage`。

```json
{
  "tool_name": "listBrowserPage",
  "arguments": {}
}
```

### openBrowserPage

打开 URL 并返回 `pageId`、`attach_url` 和页面摘要。`url` 可以是完整 URL，也可以是 `www.example.com` 这类裸域名；裸域名默认按 HTTPS 打开，本地裸地址默认按 HTTP 打开。

```json
{
  "tool_name": "openBrowserPage",
  "arguments": {
    "url": "https://example.com",
    "forceNew": false
  }
}
```

### readPage

读取当前活动标签页的文本和可交互元素。返回的元素行包含 `ref`，后续可直接用这个 `ref` 操控元素。`ref` 绑定 `document_revision`；页面导航或动态节点移除后若提示 ref 失效，应立即重新调用 `readPage`，不要重复提交旧 ref。

```json
{
  "tool_name": "readPage",
  "arguments": {
    "pageId": "browser_xxx"
  }
}
```

### clickElement

点击元素。优先使用 `readPage` 返回的 `ref`，没有 ref 时可以用 Playwright `selector`。

```json
{
  "tool_name": "clickElement",
  "arguments": {
    "pageId": "browser_xxx",
    "ref": "e1",
    "selector": "#submit",
    "element": "提交按钮",
    "dblClick": false,
    "button": "left"
  }
}
```

`ref` 和 `selector` 至少提供一个。

### typeInPage

输入文本或按键。

```json
{
  "tool_name": "typeInPage",
  "arguments": {
    "pageId": "browser_xxx",
    "ref": "e2",
    "selector": "input[name=q]",
    "element": "搜索框",
    "text": "hello",
    "submit": true,
    "key": "Enter"
  }
}
```

`text` 和 `key` 至少提供一个。传 `key` 时会按键；传 `text` 时会输入文本。

### hoverElement

悬停元素。

```json
{
  "tool_name": "hoverElement",
  "arguments": {
    "pageId": "browser_xxx",
    "ref": "e3",
    "selector": ".menu",
    "element": "菜单"
  }
}
```

### dragElement

拖拽元素到另一个元素。

```json
{
  "tool_name": "dragElement",
  "arguments": {
    "pageId": "browser_xxx",
    "fromRef": "e4",
    "fromSelector": "#source",
    "fromElement": "拖拽卡片",
    "toRef": "e5",
    "toSelector": "#target",
    "toElement": "放置区域"
  }
}
```

来源和目标都必须分别提供 ref 或 selector。

### handleDialog

响应 alert、confirm、prompt 或文件选择对话框。

```json
{
  "tool_name": "handleDialog",
  "arguments": {
    "pageId": "browser_xxx",
    "acceptModal": true,
    "promptText": "输入给 prompt 的文本",
    "selectFiles": ["/absolute/path/to/file.txt"]
  }
}
```

`selectFiles` 用于文件选择对话框，不能和 `acceptModal` / `promptText` 同时使用。

### navigatePage

跳转 URL、后退、前进、刷新，或管理标签页。`type=url` 时的 `url` 支持完整 URL、裸域名和本地裸地址。

```json
{
  "tool_name": "navigatePage",
  "arguments": {
    "pageId": "browser_xxx",
    "type": "url",
    "url": "https://example.com"
  }
}
```

`type` 可取 `url`、`back`、`forward`、`reload`、`new_tab`、`activate_tab`、`close_tab`。后两者必须传 `tabId`；标签页 ID 来自 `readPage.pages[].page_id`。`new_tab` 可选传 `url`。

### screenshotPage

截图页面或元素。返回保存在工作区 `.boxteam/browser-manager/screenshots/` 下的图片路径。

```json
{
  "tool_name": "screenshotPage",
  "arguments": {
    "pageId": "browser_xxx",
    "ref": "e6",
    "selector": "#chart",
    "element": "图表",
    "scrollIntoViewIfNeeded": true
  }
}
```

省略 `ref` 和 `selector` 时截图整个视口。

### runPlaywrightCode

对页面执行 Playwright JS 代码。只有其它浏览器工具不足时使用。代码必须通过 `page` 对象访问页面。

```json
{
  "tool_name": "runPlaywrightCode",
  "arguments": {
    "pageId": "browser_xxx",
    "code": "return await page.title();",
    "timeoutMs": 5000
  }
}
```
