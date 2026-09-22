import { describe, expect, test } from "bun:test";
import {
  formatBrowserElementSelections,
  parseBrowserElementSelectionBundle,
  parseBrowserElementSelectedMessage,
} from "./browserElementSelection";

const element = {
  ref: "e1",
  selector: '[data-boxteam-ref="e1"]',
  tag: "button",
  role: "button",
  type: "submit",
  text: "提交",
  title: "表单",
  url: "https://example.com/form",
  id: "submit",
  classes: "primary",
  outerHTML: '<button id="submit" class="primary">提交</button>',
  computedStyle: "button { color: rgb(0, 0, 0); }",
  attributes: { id: "submit", class: "primary" },
  ancestors: [
    { tagName: "html", classNames: [] },
    { tagName: "body", classNames: [] },
    { tagName: "button", id: "submit", classNames: ["primary"] },
  ],
  bounds: { x: 10, y: 20, width: 80, height: 32 },
  dimensions: { top: 20, left: 10, width: 80, height: 32 },
};

const message = {
  type: "boxteam:browser-element-selected",
  workspaceId: "workspace-1",
  browserId: "browser-1",
  element,
};

describe("浏览器元素选择消息", () => {
  test("解析完整的 VS Code 元素返回类型", () => {
    const selection = parseBrowserElementSelectedMessage(message);
    expect(selection?.ref).toBe("e1");
    expect(selection?.outerHTML).toBe(element.outerHTML);
    expect(selection?.computedStyle).toBe(element.computedStyle);
    expect(selection?.ancestors).toHaveLength(3);
  });

  test("复制内容与 VS Code 元素 Markdown 完全同构，不包含额外元数据", () => {
    const selection = parseBrowserElementSelectedMessage(message);
    const output = formatBrowserElementSelections(selection ? [selection] : []);
    expect(output).toBe([
      "Attached Element Context from Integrated Browser",
      "Element: button#submit.primary",
      "URL: https://example.com/form",
      "HTML Path: html > body > button#submit.primary",
      "Outer HTML:\n```html\n<button id=\"submit\" class=\"primary\">提交</button>\n```",
      "Dimensions:\n- top: 20px\n- left: 10px\n- width: 80px\n- height: 32px",
      "CSS:\n```css\nbutton { color: rgb(0, 0, 0); }\n```",
    ].join("\n\n"));
    expect(output).not.toContain("browser_id");
    expect(output).not.toContain("selector:");
    expect(output).not.toContain("属性:");
    expect(output).not.toContain("innerText:");
  });

  test("拒绝缺少 VS Code 核心返回字段的消息", () => {
    expect(parseBrowserElementSelectedMessage({
      ...message,
      element: { ...element, computedStyle: undefined },
    })).toBeNull();
  });

  test("选择元素+使用 rich 模式但仍只携带一个元素", () => {
    const bundle = parseBrowserElementSelectionBundle({
      ...message,
      mode: "rich",
    });
    expect(bundle?.mode).toBe("rich");
    expect(bundle?.elements).toHaveLength(1);
  });
});
