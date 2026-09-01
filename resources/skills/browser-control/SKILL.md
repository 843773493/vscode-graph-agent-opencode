---
name: browser-control
description: 用于发现、打开、读取、导航和操控浏览器页面，以及处理对话框、截图和执行 Playwright。用户要求使用 listBrowserPage、openBrowserPage、readPage、navigatePage、clickElement、typeInPage、hoverElement、dragElement、handleDialog、screenshotPage 或 runPlaywrightCode 时使用。
allowed-tools: listBrowserPage, openBrowserPage, readPage, screenshotPage, navigatePage, clickElement, typeInPage, hoverElement, dragElement, handleDialog, runPlaywrightCode
---

# 浏览器扩展工具

## 调用约定

- 所有工具都必须通过固定入口 `invoke_custom_tool` 调用。
- `tool_name` 必须使用本 Skill 声明的工具名；`arguments` 必须符合对应的 `arguments_schema`。
- 工具返回的 `pageId` 是后续调用使用的浏览器实例 ID。`readPage` 返回的 `pages[].page_id` 是标签页 `tabId`。
- 用户和模型可以共享浏览器。遇到“用户锁定了浏览器”时停止重试，告知用户等待解锁。
- 任意工具返回 `pending_dialog` 或 `pending_file_chooser` 时，下一步必须先调用 `handleDialog`。
- 临时验证页面优先使用 `data:text/html;charset=utf-8,...`；不要假设浏览器管理器能访问任意本机临时端口。
- 不要读取 `.boxteam/browser-manager/browsers.json`；浏览器状态必须通过 `listBrowserPage` 获取。
- `clickElement`、`typeInPage`、`hoverElement` 和 `screenshotPage` 优先使用最近一次 `readPage` 返回的 `ref`。页面变化后，旧 `ref` 和 `document_revision` 不再可靠，应重新读取页面。
- `runPlaywrightCode` 只有在其它浏览器工具无法完成任务时才使用；代码必须通过 `page` 对象访问页面，不得绕过浏览器锁或读取内部状态文件。
- 验收本地 HTTP 服务前，先用 `openBrowserPage` 或已有页面探测任务提供的预览地址（例如 `8765`）；地址可用时直接复用，不要再次执行 `python -m http.server`。
- 若终端返回 `port_in_use`、`EADDRINUSE` 或 `Address already in use`，这是可恢复的环境状态：禁止盲重试同一端口，先复用现有服务；确实不可用时才探测并选择明确空闲端口，然后把新 URL 传给浏览器工具。

## 工具参数 schema

以下每段是目标工具的参数描述，不是新的模型工具入口。实际调用时，必须把它放入 `invoke_custom_tool.arguments`。

### listBrowserPage

```json
{
  "tool_name": "listBrowserPage",
  "arguments_schema": {
    "type": "object",
    "properties": {}
  }
}
```

只读列出当前 Session 中未删除的浏览器页面，不创建、唤醒或导航页面。

### openBrowserPage

```json
{
  "tool_name": "openBrowserPage",
  "arguments_schema": {
    "type": "object",
    "required": ["url"],
    "properties": {
      "url": {
        "type": "string",
        "description": "完整 URL 或裸域名。"
      },
      "forceNew": {
        "type": "boolean",
        "default": false,
        "description": "为 false 时优先接管当前会话的运行中浏览器；为 true 时新建浏览器。"
      }
    }
  }
}
```

### readPage

```json
{
  "tool_name": "readPage",
  "arguments_schema": {
    "type": "object",
    "required": ["pageId"],
    "properties": {
      "pageId": {
        "type": "string",
        "description": "由 openBrowserPage 或 listBrowserPage 返回的浏览器页面 ID。"
      }
    }
  }
}
```

返回页面文本、可交互元素及其 `ref`。页面变化后必须重新调用 `readPage`。

### navigatePage

```json
{
  "tool_name": "navigatePage",
  "arguments_schema": {
    "type": "object",
    "required": ["pageId"],
    "properties": {
      "pageId": {
        "type": "string"
      },
      "type": {
        "type": "string",
        "enum": ["url", "back", "forward", "reload", "new_tab", "activate_tab", "close_tab"],
        "default": "url"
      },
      "url": {
        "type": ["string", "null"],
        "default": null,
        "description": "type=url 或 new_tab 时使用。"
      },
      "tabId": {
        "type": ["string", "null"],
        "default": null,
        "description": "type=activate_tab 或 close_tab 时使用，来源是 readPage 返回的 pages[].page_id。"
      }
    }
  }
}
```

### clickElement

```json
{
  "tool_name": "clickElement",
  "arguments_schema": {
    "type": "object",
    "required": ["pageId"],
    "properties": {
      "pageId": {"type": "string"},
      "ref": {"type": ["string", "null"], "default": null},
      "selector": {"type": ["string", "null"], "default": null},
      "element": {"type": ["string", "null"], "default": null},
      "dblClick": {"type": "boolean", "default": false},
      "button": {
        "type": "string",
        "enum": ["left", "right", "middle"],
        "default": "left"
      }
    }
  }
}
```

`ref` 和 `selector` 至少提供一个；优先使用 `ref`。

### typeInPage

```json
{
  "tool_name": "typeInPage",
  "arguments_schema": {
    "type": "object",
    "required": ["pageId"],
    "properties": {
      "pageId": {"type": "string"},
      "ref": {"type": ["string", "null"], "default": null},
      "selector": {"type": ["string", "null"], "default": null},
      "element": {"type": ["string", "null"], "default": null},
      "text": {"type": ["string", "null"], "default": null},
      "submit": {"type": "boolean", "default": false},
      "key": {"type": ["string", "null"], "default": null}
    }
  }
}
```

目标元素优先使用 `ref`；`ref`、`selector` 至少提供一个，`text` 和 `key` 至少提供一个。

### hoverElement

```json
{
  "tool_name": "hoverElement",
  "arguments_schema": {
    "type": "object",
    "required": ["pageId"],
    "properties": {
      "pageId": {"type": "string"},
      "ref": {"type": ["string", "null"], "default": null},
      "selector": {"type": ["string", "null"], "default": null},
      "element": {"type": ["string", "null"], "default": null}
    }
  }
}
```

`ref` 和 `selector` 至少提供一个；优先使用 `ref`。

### dragElement

```json
{
  "tool_name": "dragElement",
  "arguments_schema": {
    "type": "object",
    "required": ["pageId"],
    "properties": {
      "pageId": {"type": "string"},
      "fromRef": {"type": ["string", "null"], "default": null},
      "fromSelector": {"type": ["string", "null"], "default": null},
      "fromElement": {"type": ["string", "null"], "default": null},
      "toRef": {"type": ["string", "null"], "default": null},
      "toSelector": {"type": ["string", "null"], "default": null},
      "toElement": {"type": ["string", "null"], "default": null}
    }
  }
}
```

来源和目标必须分别至少提供一个 `ref` 或 `selector`；优先使用 `ref`。

### handleDialog

```json
{
  "tool_name": "handleDialog",
  "arguments_schema": {
    "type": "object",
    "required": ["pageId"],
    "properties": {
      "pageId": {"type": "string"},
      "acceptModal": {"type": ["boolean", "null"], "default": null},
      "promptText": {"type": ["string", "null"], "default": null},
      "selectFiles": {
        "type": ["array", "null"],
        "items": {"type": "string"},
        "default": null,
        "description": "文件选择对话框使用的绝对路径列表。"
      }
    }
  }
}
```

根据返回的 `pending_dialog` 或 `pending_file_chooser` 选择对应字段；不要把文件选择路径写成相对路径。

### screenshotPage

```json
{
  "tool_name": "screenshotPage",
  "arguments_schema": {
    "type": "object",
    "required": ["pageId"],
    "properties": {
      "pageId": {"type": "string"},
      "ref": {"type": ["string", "null"], "default": null},
      "selector": {"type": ["string", "null"], "default": null},
      "element": {"type": ["string", "null"], "default": null},
      "scrollIntoViewIfNeeded": {"type": "boolean", "default": false}
    }
  }
}
```

省略 `ref` 和 `selector` 时截图整个视口。

### runPlaywrightCode

```json
{
  "tool_name": "runPlaywrightCode",
  "arguments_schema": {
    "type": "object",
    "required": ["pageId", "code"],
    "properties": {
      "pageId": {"type": "string"},
      "code": {
        "type": "string",
        "description": "通过 page 对象访问页面的 Playwright JavaScript。"
      },
      "timeoutMs": {
        "type": "integer",
        "default": 5000,
        "minimum": 1,
        "maximum": 60000
      }
    }
  }
}
```
