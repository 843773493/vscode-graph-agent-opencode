import { afterEach, describe, expect, test } from "bun:test";
import { copyTextToClipboardFromPromise } from "./clipboard";

const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
const originalClipboardItem = Object.getOwnPropertyDescriptor(
  globalThis,
  "ClipboardItem",
);

function restoreProperty(
  target: object,
  property: PropertyKey,
  descriptor: PropertyDescriptor | undefined,
): void {
  if (descriptor) {
    Object.defineProperty(target, property, descriptor);
  } else {
    Reflect.deleteProperty(target, property);
  }
}

afterEach(() => {
  restoreProperty(navigator, "clipboard", originalClipboard);
  restoreProperty(globalThis, "ClipboardItem", originalClipboardItem);
});

describe("异步文本剪贴板写入", () => {
  test("在异步内容完成前启动 ClipboardItem 写入", async () => {
    let resolveText!: (text: string) => void;
    const textPromise = new Promise<string>((resolve) => {
      resolveText = resolve;
    });
    let writeCalled = false;
    let itemData: Record<string, unknown> | undefined;

    class FakeClipboardItem {
      constructor(data: Record<string, unknown>) {
        itemData = data;
      }
    }
    Object.defineProperty(globalThis, "ClipboardItem", {
      configurable: true,
      value: FakeClipboardItem,
    });
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        write: async () => {
          writeCalled = true;
        },
      },
    });

    const copyPromise = copyTextToClipboardFromPromise(textPromise);

    expect(writeCalled).toBe(true);
    expect(itemData).toBeDefined();
    resolveText("异步生成的会话信息");
    await copyPromise;

    const blob = await itemData?.["text/plain"] as Blob;
    expect(await blob.text()).toBe("异步生成的会话信息");
  });
});
