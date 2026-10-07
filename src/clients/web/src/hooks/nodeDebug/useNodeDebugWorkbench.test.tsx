import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as api from "../../api";
import type {
  NodeDebugCapabilities,
  NodeDebugState,
  WebUiSettingsUpdate,
} from "../../types/backend";
import {
  useNodeDebugWorkbench,
  type NodeDebugWorkbenchBinding,
} from "./useNodeDebugWorkbench";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";

function state(threadId: string): NodeDebugState {
  return {
    session_id: "session-workbench",
    status: "paused",
    configurations: [],
    args: [],
    call_stack: [{
      call_frame_id: "frame-1",
      function_name: "main",
      url: "file:///workspace/app.js",
      path: "/workspace/app.js",
      line: 17,
      column: 2,
      scope_names: [],
      variables: [],
    }],
    breakpoints: [],
    output: [],
    evaluations: [],
    actions: [],
    source_changed_paths: [],
    thread_id: threadId,
  };
}

const capabilities: NodeDebugCapabilities = {
  enabled: true,
  default_adapter: "node",
  supported_adapters: ["node"],
  launch_profiles: [],
};
const mainThreadId = "thr_00000000700070008000000000000001";
const childThreadId = "thr_00000000700070008000000000000002";

const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
const originalBroadcastChannelDescriptor = Object.getOwnPropertyDescriptor(
  globalThis,
  "BroadcastChannel",
);
let renderer: ReactTestRenderer | undefined;
let restoreApi = () => {};

function installBrowserGlobals(): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      setInterval: globalThis.setInterval.bind(globalThis),
      clearInterval: globalThis.clearInterval.bind(globalThis),
    },
  });
  Object.defineProperty(globalThis, "BroadcastChannel", {
    configurable: true,
    value: undefined,
  });
}

afterEach(() => {
  act(() => renderer?.unmount());
  renderer = undefined;
  restoreApi();
  restoreGlobalDescriptor("window", originalWindowDescriptor);
  restoreGlobalDescriptor("BroadcastChannel", originalBroadcastChannelDescriptor);
});

describe("useNodeDebugWorkbench", () => {
  test("集中 owner 选择、active frame 和断点动作映射", async () => {
    installBrowserGlobals();
    const getStateSpy = spyOn(api, "getNodeDebugState").mockImplementation(
      async (_port, _sessionId, threadId) => state(threadId),
    );
    const mainThreadSpy = spyOn(api, "getSessionMainThread").mockResolvedValue({
      session_id: "session-workbench",
      thread_id: mainThreadId,
    });
    const capabilitiesSpy = spyOn(api, "getNodeDebugCapabilities").mockResolvedValue(capabilities);
    const actionSpy = spyOn(api, "applyNodeDebugAction").mockImplementation(
      async (_port, request) => state(request.thread_id),
    );
    restoreApi = () => {
      getStateSpy.mockRestore();
      mainThreadSpy.mockRestore();
      capabilitiesSpy.mockRestore();
      actionSpy.mockRestore();
    };

    let binding!: NodeDebugWorkbenchBinding;
    let auxiliaryVisible = false;
    let auxiliaryTab = "";
    const persistedLayouts: WebUiSettingsUpdate["layout"][] = [];
    const statusMessages: string[] = [];
    function Probe(): React.ReactNode {
      binding = useNodeDebugWorkbench({
        apiPort: 49_412,
        workspaceId: "workspace-workbench",
        workspaces: [{ workspace_id: "workspace-workbench", backend_workspace_id: "backend-workbench", connection_kind: "local" }],
        sessionId: "session-workbench",
        enabled: true,
        onStatusChange: (message) => statusMessages.push(message),
        extensionWindowRequested: false,
        setAuxiliaryVisible: (visible) => { auxiliaryVisible = visible; },
        setAuxiliaryTab: (tab) => { auxiliaryTab = tab; },
        persistLayoutSettings: (layout) => { persistedLayouts.push(layout); },
      });
      return null;
    }

    await act(async () => {
      renderer = create(<Probe />);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(binding.threadId).toBe(mainThreadId);
    expect(binding.activeFrame?.path).toBe("/workspace/app.js");
    expect(mainThreadSpy).toHaveBeenCalledTimes(1);

    await act(async () => {
      binding.selectThread(childThreadId);
      await Promise.resolve();
    });
    expect(binding.threadId).toBe(childThreadId);
    expect(mainThreadSpy).toHaveBeenCalledTimes(1);
    expect(auxiliaryVisible).toBe(true);
    expect(auxiliaryTab).toBe("debug");
    expect(persistedLayouts).toEqual([{
      auxiliary_visible: true,
      auxiliary_tab: "debug",
    }]);
    expect(statusMessages[statusMessages.length - 1]).toBe(`已切换调试 owner: ${childThreadId}`);

    await act(async () => {
      binding.changeBreakpoint(
        "/workspace/app.js",
        17,
        null,
        { condition: "x > 0", hit_condition: null, log_message: null },
      );
      await Promise.resolve();
    });
    expect(actionSpy).toHaveBeenLastCalledWith(
      49_412,
      {
        session_id: "session-workbench",
        thread_id: childThreadId,
        action: "set_breakpoint",
        params: {
          path: "/workspace/app.js",
          line: 17,
          condition: "x > 0",
          hit_condition: null,
          log_message: null,
        },
      },
      "workspace-workbench",
    );
    expect(getStateSpy).toHaveBeenCalled();
  });

  test("catalog main thread 解析前阻止调试 mutation", async () => {
    installBrowserGlobals();
    const mainThreadSpy = spyOn(api, "getSessionMainThread").mockReturnValue(
      new Promise(() => {}),
    );
    const startSpy = spyOn(api, "startNodeDebug").mockResolvedValue(state(""));
    restoreApi = () => {
      mainThreadSpy.mockRestore();
      startSpy.mockRestore();
    };

    let binding!: NodeDebugWorkbenchBinding;
    function Probe(): React.ReactNode {
      binding = useNodeDebugWorkbench({
        apiPort: 49_412,
        workspaceId: "workspace-workbench",
        workspaces: [{ workspace_id: "workspace-workbench", backend_workspace_id: "backend-workbench", connection_kind: "local" }],
        sessionId: "session-workbench",
        enabled: true,
        onStatusChange: () => {},
        extensionWindowRequested: false,
        setAuxiliaryVisible: () => {},
        setAuxiliaryTab: () => {},
        persistLayoutSettings: () => {},
      });
      return null;
    }

    await act(async () => {
      renderer = create(<Probe />);
      await Promise.resolve();
    });
    expect(binding.threadId).toBeNull();
    expect(await binding.controller.start({ path: "entry.mjs" })).toBeNull();
    expect(startSpy).not.toHaveBeenCalled();
  });
});
