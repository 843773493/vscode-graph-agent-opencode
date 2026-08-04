import { describe, expect, test } from "bun:test";

import {
  installTerminalPasteGuard,
  installTerminalShortcuts,
  terminalShortcutAction,
} from "./terminalShortcuts.js";

function keyEvent(key, overrides = {}) {
  return {
    type: "keydown",
    key,
    ctrlKey: false,
    shiftKey: false,
    altKey: false,
    metaKey: false,
    ...overrides,
  };
}

describe("terminalShortcutAction", () => {
  test("Linux 使用 Ctrl+Shift 复制粘贴和 Shift+Insert", () => {
    expect(
      terminalShortcutAction(keyEvent("c", { ctrlKey: true, shiftKey: true }), {
        platform: "linux",
        hasSelection: true,
      }),
    ).toBe("copy");
    expect(
      terminalShortcutAction(keyEvent("v", { ctrlKey: true, shiftKey: true }), {
        platform: "linux",
        hasSelection: false,
      }),
    ).toBe("paste");
    expect(
      terminalShortcutAction(keyEvent("Insert", { shiftKey: true }), {
        platform: "linux",
        hasSelection: false,
      }),
    ).toBe("paste");
  });

  test("Linux 保留 Shell 的 Ctrl+C 与 Ctrl+V", () => {
    expect(
      terminalShortcutAction(keyEvent("c", { ctrlKey: true }), {
        platform: "linux",
        hasSelection: true,
      }),
    ).toBeNull();
    expect(
      terminalShortcutAction(keyEvent("v", { ctrlKey: true }), {
        platform: "linux",
        hasSelection: false,
      }),
    ).toBeNull();
  });

  test("Windows 有选区时 Ctrl+C 复制，无选区时发送中断", () => {
    expect(
      terminalShortcutAction(keyEvent("c", { ctrlKey: true }), {
        platform: "windows",
        hasSelection: true,
      }),
    ).toBe("copy");
    expect(
      terminalShortcutAction(keyEvent("c", { ctrlKey: true }), {
        platform: "windows",
        hasSelection: false,
      }),
    ).toBeNull();
    expect(
      terminalShortcutAction(keyEvent("v", { ctrlKey: true }), {
        platform: "windows",
        hasSelection: false,
      }),
    ).toBe("paste");
  });

  test("macOS 使用 Command 快捷键", () => {
    expect(
      terminalShortcutAction(keyEvent("c", { metaKey: true }), {
        platform: "mac",
        hasSelection: true,
      }),
    ).toBe("copy");
    expect(
      terminalShortcutAction(keyEvent("v", { metaKey: true }), {
        platform: "mac",
        hasSelection: false,
      }),
    ).toBe("paste");
    expect(
      terminalShortcutAction(keyEvent("a", { metaKey: true }), {
        platform: "mac",
        hasSelection: false,
      }),
    ).toBe("selectAll");
    expect(
      terminalShortcutAction(keyEvent("f", { metaKey: true }), {
        platform: "mac",
        hasSelection: false,
      }),
    ).toBe("search");
  });

  test("Windows/Linux 使用 Ctrl+F 搜索且忽略 keyup", () => {
    expect(
      terminalShortcutAction(keyEvent("f", { ctrlKey: true }), {
        platform: "windows",
        hasSelection: false,
      }),
    ).toBe("search");
    expect(
      terminalShortcutAction(keyEvent("f", { ctrlKey: true }), {
        platform: "linux",
        hasSelection: false,
      }),
    ).toBe("search");
    expect(
      terminalShortcutAction(
        keyEvent("f", { type: "keyup", ctrlKey: true }),
        { platform: "windows", hasSelection: false },
      ),
    ).toBeNull();
  });

  test("搜索快捷键阻止浏览器默认行为并执行终端动作", () => {
    let handler = null;
    let searched = 0;
    let prevented = 0;
    let stopped = 0;
    installTerminalShortcuts({
      terminal: {
        element: {
          addEventListener: () => {},
        },
        attachCustomKeyEventHandler: (nextHandler) => {
          handler = nextHandler;
        },
        hasSelection: () => true,
      },
      platform: "windows",
      actions: {
        search: () => {
          searched += 1;
        },
      },
    });
    const event = keyEvent("f", {
      ctrlKey: true,
      preventDefault: () => {
        prevented += 1;
      },
      stopPropagation: () => {
        stopped += 1;
      },
    });

    expect(handler(event)).toBe(false);
    expect(searched).toBe(1);
    expect(prevented).toBe(1);
    expect(stopped).toBe(1);
  });

  test("复制粘贴快捷键交给浏览器原生事件", () => {
    let handler = null;
    let captureHandler = null;
    let prevented = 0;
    let stopped = 0;
    let hasSelection = false;
    installTerminalShortcuts({
      terminal: {
        element: {
          addEventListener: (type, nextHandler, capture) => {
            expect(type).toBe("keydown");
            expect(capture).toBe(true);
            captureHandler = nextHandler;
          },
        },
        attachCustomKeyEventHandler: (nextHandler) => {
          handler = nextHandler;
        },
        hasSelection: () => hasSelection,
      },
      platform: "windows",
      actions: {},
    });
    const pasteEvent = keyEvent("v", {
      ctrlKey: true,
      preventDefault: () => {
        prevented += 1;
      },
      stopPropagation: () => {
        stopped += 1;
      },
    });

    captureHandler(pasteEvent);
    expect(handler(pasteEvent)).toBe(false);
    expect(stopped).toBe(1);
    hasSelection = true;
    const copyEvent = keyEvent("c", {
      ctrlKey: true,
      stopPropagation: () => {
        stopped += 1;
      },
    });
    captureHandler(copyEvent);
    expect(handler(copyEvent)).toBe(false);
    expect(stopped).toBe(2);
    expect(prevented).toBe(0);
  });

  test("未连接时阻止原生粘贴并明确提示", () => {
    let pasteHandler = null;
    let attached = false;
    let blocked = 0;
    let focused = 0;
    const terminal = {
      element: {
        addEventListener: (type, handler, capture) => {
          expect(type).toBe("paste");
          expect(capture).toBe(true);
          pasteHandler = handler;
        },
      },
      focus: () => {
        focused += 1;
      },
    };
    installTerminalPasteGuard({
      terminal,
      getAttached: () => attached,
      onBlocked: () => {
        blocked += 1;
      },
    });
    let prevented = 0;
    let stopped = 0;
    pasteHandler({
      preventDefault: () => {
        prevented += 1;
      },
      stopPropagation: () => {
        stopped += 1;
      },
    });

    expect(blocked).toBe(1);
    expect(focused).toBe(1);
    expect(prevented).toBe(1);
    expect(stopped).toBe(1);

    attached = true;
    pasteHandler({
      preventDefault: () => {
        prevented += 1;
      },
      stopPropagation: () => {
        stopped += 1;
      },
    });
    expect(blocked).toBe(1);
    expect(prevented).toBe(1);
    expect(stopped).toBe(1);
  });
});
