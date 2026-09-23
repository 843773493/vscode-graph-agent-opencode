import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as apiBarrel from "../../api";
import { SESSION_STREAM_MAX_RECONNECT_ATTEMPTS } from "../sessionEventStream/sessionEventStreamPolicy";
import { useWorkspaceFileWatch } from "./useWorkspaceFileWatch";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";

const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

/** 把 window.setTimeout 压成 0 延迟，避免真实退避等待把测试拖成分钟级。 */
function installWindow(): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      setTimeout: (handler: () => void) => globalThis.setTimeout(handler, 0),
      clearTimeout: (id: number) => globalThis.clearTimeout(id),
    },
  });
}

afterEach(() => {
  restoreGlobalDescriptor("window", originalWindowDescriptor);
});

async function flush(): Promise<void> {
  await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  });
}

async function mountFileWatch(
  onStatusChange: (message: string) => void,
): Promise<ReactTestRenderer> {
  function Harness(): React.ReactNode {
    useWorkspaceFileWatch({
      active: true,
      port: 49_740,
      workspaceId: "ws-file-watch",
      paths: ["/workspace"],
      onOverflow: () => undefined,
      onStatusChange,
    });
    return null;
  }
  let renderer: ReactTestRenderer | undefined;
  await act(async () => {
    renderer = create(<Harness />);
  });
  return renderer!;
}

describe("useWorkspaceFileWatch 有界重连", () => {
  test("连续建连失败到达上限后停止重连并给出可见终态", async () => {
    installWindow();
    const streamSpy = spyOn(apiBarrel, "streamWorkspaceFileEvents")
      .mockRejectedValue(new Error("连接被拒绝"));
    const statuses: string[] = [];
    const renderer = await mountFileWatch((message) => statuses.push(message));

    // 冲刷远超上限的轮次：若上限判定缺失，重连次数会继续无界增长。
    for (let round = 0; round < SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 10; round += 1) {
      await flush();
    }

    expect(streamSpy).toHaveBeenCalledTimes(SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 1);
    expect(statuses[statuses.length - 1]).toContain("已停止自动重连");
    streamSpy.mockRestore();
    act(() => renderer.unmount());
  });

  test("连接建立并持续有批次活动时不会被上限误判为放弃", async () => {
    installWindow();
    // 每次建连都立即报告 onConnected 后断开；onConnected 归零计数，因此
    // 连续远超上限次「先建立后断开」必须一直重连。
    const streamSpy = spyOn(apiBarrel, "streamWorkspaceFileEvents")
      .mockImplementation(async (_port, _paths, options) => {
        options?.onConnected?.();
        throw new Error("断开");
      });
    const statuses: string[] = [];
    const renderer = await mountFileWatch((message) => statuses.push(message));

    for (let round = 0; round < SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 10; round += 1) {
      await flush();
    }

    expect(streamSpy.mock.calls.length)
      .toBeGreaterThan(SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 1);
    expect(statuses.some((text) => text.includes("已停止自动重连"))).toBe(false);
    streamSpy.mockRestore();
    act(() => renderer.unmount());
  });
});
