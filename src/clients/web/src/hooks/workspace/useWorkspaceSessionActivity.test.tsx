import { afterEach, describe, expect, jest, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as sessionActivityApi from "../../api/session/sessionActivity";
import * as sessionRefresh from "../sessionEventStream/sessionRefresh";
import { SESSION_STREAM_MAX_RECONNECT_ATTEMPTS } from "../sessionEventStream/sessionEventStreamPolicy";
import type { AppState } from "../../types/frontend";
import type { SessionActivity } from "../../types/backend";
import { useWorkspaceSessionActivity } from "./useWorkspaceSessionActivity";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";

const API_PORT = 49_712;
const WORKSPACE_ID = "ws-activity";

const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
const originalDocumentDescriptor = Object.getOwnPropertyDescriptor(globalThis, "document");

/** 把 window.setTimeout 压成 0 延迟，避免真实退避等待把测试拖成分钟级。 */
function installWindow(): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      setTimeout: (handler: () => void) => globalThis.setTimeout(handler, 0),
      clearTimeout: (id: number) => globalThis.clearTimeout(id),
    },
  });
  installDocument();
}

/**
 * 假定时器版 window：保留真实退避延迟，让测试用 jest.advanceTimersByTime
 * 精确推进到「退避等待中」这一时刻，不必真实等待 1 秒以上。
 */
function installControlledWindow(): void {
  jest.useFakeTimers();
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      setTimeout: (handler: () => void, delayMs?: number) =>
        globalThis.setTimeout(handler, delayMs),
      clearTimeout: (id: number) => globalThis.clearTimeout(id),
    },
  });
  installDocument();
}

function installDocument(): void {
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: {
      visibilityState: "hidden",
      hasFocus: () => false,
    },
  });
}

function activity(eventId: string): SessionActivity {
  return { event_id: eventId, session_id: "session-1" } as unknown as SessionActivity;
}

function appState(): AppState {
  return { status: "准备就绪" } as unknown as AppState;
}

let renderer: ReactTestRenderer | undefined;

async function mountActivity(
  state: AppState,
  onChange: (next: AppState) => void,
): Promise<void> {
  function Probe(): React.ReactNode {
    useWorkspaceSessionActivity({
      apiPort: API_PORT,
      workspaceId: WORKSPACE_ID,
      currentSessionCacheKey: null,
      setState: (update) => {
        const next = typeof update === "function"
          ? (update as (previous: AppState) => AppState)(state)
          : update;
        Object.assign(state, next);
        onChange(next);
      },
    });
    return null;
  }
  await act(async () => {
    renderer = create(<Probe />);
  });
}

async function flush(): Promise<void> {
  await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  });
}

/** 只排空微任务，不推进任何定时器。 */
async function drainMicrotasks(): Promise<void> {
  await act(async () => {
    for (let round = 0; round < 50; round += 1) {
      await Promise.resolve();
    }
  });
}

afterEach(() => {
  jest.useRealTimers();
  if (renderer) {
    act(() => renderer!.unmount());
    renderer = undefined;
  }
  restoreGlobalDescriptor("window", originalWindowDescriptor);
  restoreGlobalDescriptor("document", originalDocumentDescriptor);
});

describe("useWorkspaceSessionActivity 重连与卸载", () => {
  test("流持续关闭时有界重连，耗尽后给可见终态而不是无限重连", async () => {
    installWindow();
    const listSpy = spyOn(sessionActivityApi, "listSessionActivity")
      .mockResolvedValue({ items: [], next_cursor: 0 } as never);
    const streamSpy = spyOn(sessionActivityApi, "streamSessionActivity")
      .mockResolvedValue(undefined);
    const state = appState();
    await mountActivity(state, () => undefined);

    // 冲刷到重连上限耗尽。
    for (let round = 0; round < SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 3; round += 1) {
      await flush();
    }

    expect(streamSpy).toHaveBeenCalledTimes(SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 1);
    expect(state.status).toContain("已停止自动重连");
    listSpy.mockRestore();
    streamSpy.mockRestore();
  });

  test("刷新请求在卸载后才失败时不再写状态", async () => {
    installWindow();
    let rejectRefresh!: (cause: unknown) => void;
    const pendingRefresh = new Promise<void>((_resolve, reject) => {
      rejectRefresh = reject;
    });
    const listSpy = spyOn(sessionActivityApi, "listSessionActivity")
      .mockResolvedValue({ items: [], next_cursor: 0 } as never);
    const streamSpy = spyOn(sessionActivityApi, "streamSessionActivity")
      .mockImplementation(async (_port, _workspaceId, options) => {
        // 先送一个活动事件触发会话摘要刷新，再抛错进入重连分支。
        options?.onEvent?.(activity("evt-1"), 1);
        throw new Error("断开");
      });
    const refreshSpy = spyOn(sessionRefresh, "refreshWorkspaceSessionList")
      .mockReturnValue(pendingRefresh as never);
    const state = appState();
    await mountActivity(state, () => undefined);

    await flush();
    expect(refreshSpy).toHaveBeenCalled();
    const statusBeforeUnmount = state.status;

    act(() => renderer!.unmount());
    renderer = undefined;
    // 卸载后刷新才失败：绝不能把失败写回已卸载组件。
    rejectRefresh(new Error("卸载后的刷新失败"));
    await flush();

    expect(state.status).toBe(statusBeforeUnmount);
    expect(state.status).not.toContain("刷新会话摘要失败");
    listSpy.mockRestore();
    streamSpy.mockRestore();
    refreshSpy.mockRestore();
  });

  test("退避等待期间卸载时不再发起新的会话活动订阅", async () => {
    installControlledWindow();
    const listSpy = spyOn(sessionActivityApi, "listSessionActivity")
      .mockResolvedValue({ items: [], next_cursor: 0 } as never);
    const streamSpy = spyOn(sessionActivityApi, "streamSessionActivity")
      .mockRejectedValue(new Error("断开"));
    await mountActivity(appState(), () => undefined);

    // 首轮 list + stream 只依赖微任务；排空后即停在 waitForReconnect 退避等待中。
    await drainMicrotasks();
    expect(streamSpy.mock.calls.length).toBe(1);
    const listsAtUnmount = listSpy.mock.calls.length;
    const streamsAtUnmount = streamSpy.mock.calls.length;
    act(() => {
      renderer!.unmount();
      renderer = undefined;
    });
    // 卸载会 abort；waitForReconnect 的 abort 监听同步兑现，若不复查 abort 就
    // 递归重连，这里会在没有推进任何定时器的情况下再发出 list + stream。
    await drainMicrotasks();

    expect(listSpy.mock.calls.length).toBe(listsAtUnmount);
    expect(streamSpy.mock.calls.length).toBe(streamsAtUnmount);
    listSpy.mockRestore();
    streamSpy.mockRestore();
  });
});
