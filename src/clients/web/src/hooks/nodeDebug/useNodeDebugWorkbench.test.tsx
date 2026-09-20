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

function restoreGlobal(
  name: "window" | "BroadcastChannel",
  descriptor: PropertyDescriptor | undefined,
): void {
  if (descriptor) {
    Object.defineProperty(globalThis, name, descriptor);
  } else {
    Reflect.deleteProperty(globalThis, name);
  }
}

afterEach(() => {
  act(() => renderer?.unmount());
  renderer = undefined;
  restoreApi();
  restoreGlobal("window", originalWindowDescriptor);
  restoreGlobal("BroadcastChannel", originalBroadcastChannelDescriptor);
});

describe("useNodeDebugWorkbench", () => {
  test("集中 owner 选择、active frame 和断点动作映射", async () => {
    installBrowserGlobals();
    const getStateSpy = spyOn(api, "getNodeDebugState").mockImplementation(
      async (_port, _sessionId, threadId) => state(threadId),
    );
    const capabilitiesSpy = spyOn(api, "getNodeDebugCapabilities").mockResolvedValue(capabilities);
    const actionSpy = spyOn(api, "applyNodeDebugAction").mockImplementation(
      async (_port, request) => state(request.thread_id),
    );
    restoreApi = () => {
      getStateSpy.mockRestore();
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
    expect(binding.threadId).toBe("main");
    expect(binding.activeFrame?.path).toBe("/workspace/app.js");

    await act(async () => {
      binding.selectThread("child-1");
      await Promise.resolve();
    });
    expect(binding.threadId).toBe("child-1");
    expect(auxiliaryVisible).toBe(true);
    expect(auxiliaryTab).toBe("debug");
    expect(persistedLayouts).toEqual([{
      auxiliary_visible: true,
      auxiliary_tab: "debug",
    }]);
    expect(statusMessages[statusMessages.length - 1]).toBe("已切换调试 owner: child-1");

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
        thread_id: "child-1",
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
});
