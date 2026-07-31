import { describe, expect, test } from "bun:test";
import { clickElement, handleDialog } from "./browserPageActions.js";

describe("用户处理浏览器文件选择", () => {
  test("将用户设备文件以内存载荷交给 Playwright", async () => {
    let selectedFiles = null;
    const session = {
      pendingDialog: null,
      pendingFileChooser: {
        setFiles: async (files) => {
          selectedFiles = files;
        },
      },
    };

    const result = await handleDialog(session, {
      filePayloads: [{
        name: "hello.txt",
        mimeType: "text/plain",
        data: Buffer.from("你好").toString("base64"),
      }],
    });

    expect(result.summary).toContain("1 个文件");
    expect(selectedFiles[0].name).toBe("hello.txt");
    expect(selectedFiles[0].buffer.toString("utf8")).toBe("你好");
    expect(session.pendingFileChooser).toBeNull();
  });
});

describe("失效元素 ref", () => {
  test("目标已移除时立即返回专用错误", async () => {
    const locator = {
      count: async () => 0,
      click: async () => {
        throw new Error("不应执行 click");
      },
    };
    const page = { locator: () => ({ first: () => locator }) };
    const refs = new Map([["r1_e1", { selector: "[data-boxteam-ref=r1_e1]" }]]);

    try {
      await clickElement(page, refs, { ref: "r1_e1" });
      throw new Error("预期 clickElement 失败");
    } catch (error) {
      expect(error.code).toBe("browser_stale_element_ref");
      expect(error.message).toContain("请重新调用 readPage");
    }
  });
});
