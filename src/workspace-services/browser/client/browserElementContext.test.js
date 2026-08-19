import { describe, expect, test } from "bun:test";
import {
  createBrowserElementContextValue,
  formatBrowserElementClipboard,
} from "./browserElementContext.js";

const element = {
  tag: "button",
  id: "submit",
  classes: "primary",
  url: "https://example.com/form",
  outerHTML: '<button id="submit" class="primary">提交</button>',
  computedStyle: "button { color: rgb(0, 0, 0); }",
  ancestors: [
    { tagName: "html", classNames: [] },
    { tagName: "body", classNames: [] },
    { tagName: "button", id: "submit", classNames: ["primary"] },
  ],
  bounds: { x: 10, y: 20, width: 80, height: 32 },
  dimensions: { top: 20, left: 10, width: 80, height: 32 },
};

describe("浏览器元素剪贴板上下文", () => {
  test("生成与 VS Code 相同的固定 Markdown 区块", () => {
    expect(createBrowserElementContextValue(element)).toBe([
      "Attached Element Context from Integrated Browser",
      "Element: button#submit.primary",
      "URL: https://example.com/form",
      "HTML Path: html > body > button#submit.primary",
      "Outer HTML:\n```html\n<button id=\"submit\" class=\"primary\">提交</button>\n```",
      "Dimensions:\n- top: 20px\n- left: 10px\n- width: 80px\n- height: 32px",
      "CSS:\n```css\nbutton { color: rgb(0, 0, 0); }\n```",
    ].join("\n\n"));
  });

  test("多个输入只按区块拼接，不增加数组 JSON 包装", () => {
    const output = formatBrowserElementClipboard([element, element]);
    expect(output.match(/Attached Element Context from Integrated Browser/g)).toHaveLength(2);
    expect(output.startsWith("["))
      .toBe(false);
    expect(output).not.toContain("browser_id");
  });
});
