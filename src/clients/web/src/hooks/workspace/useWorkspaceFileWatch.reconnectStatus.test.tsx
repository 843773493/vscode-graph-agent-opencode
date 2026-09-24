import { afterEach, describe, expect, jest, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import * as api from "../../api";
import { useWorkspaceFileWatch } from "./useWorkspaceFileWatch";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";

/**
 * 文件监听流重连成功后的状态一致性。
 *
 * 真实浏览器审查记录：断流后状态栏出现「文件监听流意外结束；正在重连」，撤掉拦截后
 * SSE 确实重建成功（放行建连计数 = 1），但 20s、40s 后仍显示同一条「正在重连」文案。
 * 旧实现只在 onConnected 里重置退避计数，没有清除先前写入的状态文案，形成
 * 「状态与事实不一致」——按 AGENTS.md 前端状态管理原则，成功后必须对齐状态。
 */

const PORT = 49_503;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

/** 退避等待压成 0 延迟，避免真实 500ms 起步的等待拖慢用例。 */
function installWindow(): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      setTimeout: (handler: () => void) => globalThis.setTimeout(handler, 0),
      clearTimeout: (id: number) => globalThis.clearTimeout(id),
    },
  });
}

let renderer: ReactTestRenderer | undefined;

async function flush(): Promise<void> {
  await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  });
}

async function mountWatch(onStatusChange: (message: string) => void): Promise<void> {
  function Probe(): React.ReactNode {
    useWorkspaceFileWatch({
      active: true,
      port: PORT,
      workspaceId: "ws_watch",
      paths: ["/tmp/shortcut"],
      onOverflow: () => undefined,
      onStatusChange,
    });
    return null;
  }
  await act(async () => {
    renderer = create(<Probe />);
  });
}

afterEach(() => {
  jest.useRealTimers();
  if (renderer) {
    act(() => renderer!.unmount());
    renderer = undefined;
  }
  restoreGlobalDescriptor("window", originalWindowDescriptor);
});

describe("useWorkspaceFileWatch 重连后的状态对齐", () => {
  test("重连成功后必须清除先前的「正在重连」状态，而不是永久残留", async () => {
    installWindow();
    const statuses: string[] = [];
    let connectCount = 0;
    const streamSpy = spyOn(api, "streamWorkspaceFileEvents")
      .mockImplementation(async (_port, _paths, options) => {
        connectCount += 1;
        if (connectCount === 1) {
          // 首次连接：流意外结束，进入重连分支并写入「正在重连」。
          throw new Error("文件监听流意外结束");
        }
        // 第二次连接：真实建立成功，随后保持挂起直到卸载。
        options?.onConnected?.();
        await new Promise<void>((resolve) => {
          options?.signal?.addEventListener("abort", () => resolve());
        });
      });

    await mountWatch((message) => statuses.push(message));
    await flush();
    await flush();

    // 重连确实发生过，且失败文案已写入。
    expect(connectCount).toBeGreaterThanOrEqual(2);
    expect(statuses.some((message) => message.includes("正在重连"))).toBe(true);

    // 重连成功后必须清除该文案：再冲刷若干轮，不得仍以「正在重连」结尾。
    for (let round = 0; round < 4; round += 1) await flush();
    // 不用 Array.prototype.at（tsconfig lib 为 ES2021，类型层面不存在）。
    const lastStatus = statuses[statuses.length - 1] ?? "";
    expect(lastStatus).not.toContain("正在重连");
    expect(lastStatus).toContain("已恢复");

    streamSpy.mockRestore();
  });

  test("重连成功后清除文案，且未经历断流时不写多余状态", async () => {
    installWindow();
    const statuses: string[] = [];
    const streamSpy = spyOn(api, "streamWorkspaceFileEvents")
      .mockImplementation(async (_port, _paths, options) => {
        options?.onConnected?.();
        await new Promise<void>((resolve) => {
          options?.signal?.addEventListener("abort", () => resolve());
        });
      });

    await mountWatch((message) => statuses.push(message));
    await flush();
    await flush();

    // 一次成功建连不该宣称「已恢复」——只有真的从断流里接回来才写恢复文案。
    expect(statuses).toEqual([]);

    streamSpy.mockRestore();
  });
});
