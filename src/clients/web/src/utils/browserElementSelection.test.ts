import { describe, expect, test } from "bun:test";
import {
  formatBrowserElementSelections,
  parseBrowserElementSelectedMessage,
} from "./browserElementSelection";

const message = {
  type: "boxteam:browser-element-selected",
  workspaceId: "workspace-1",
  browserId: "browser-1",
  element: {
    ref: "e1",
    selector: '[data-boxteam-ref="e1"]',
    tag: "button",
    role: "button",
    type: "submit",
    text: "提交",
    title: "表单",
    url: "https://example.com/form",
  },
};

describe("浏览器元素选择消息", () => {
  test("解析并格式化模型可用的元素上下文", () => {
    const selection = parseBrowserElementSelectedMessage(message);
    expect(selection?.ref).toBe("e1");
    expect(formatBrowserElementSelections(selection ? [selection] : []))
      .toContain("browser_id=browser-1 ref=e1");
  });

  test("拒绝字段不完整的消息", () => {
    expect(parseBrowserElementSelectedMessage({ ...message, element: { ref: "e1" } }))
      .toBeNull();
  });
});
