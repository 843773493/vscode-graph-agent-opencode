import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as api from "../../api";
import type {
  NodeDebugCapabilities,
  NodeDebugState,
} from "../../types/backend";
import { NodeDebugMutationGate } from "./nodeDebugMutationGate";
import {
  useNodeDebugStateSync,
  type NodeDebugStateSync,
} from "./useNodeDebugStateSync";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (cause: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function state(threadId: string, status: NodeDebugState["status"] = "idle"): NodeDebugState {
  return {
    session_id: "session-state-sync",
    status,
    configurations: [],
    args: [],
    call_stack: [],
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
  default_adapter: "debugpy",
  supported_adapters: [],
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

function installApiSpies(pending: Deferred<NodeDebugState>[]): void {
  const stateSpy = spyOn(api, "getNodeDebugState").mockImplementation(
    async () => {
      const next = deferred<NodeDebugState>();
      pending.push(next);
      return next.promise;
    },
  );
  const capabilitiesSpy = spyOn(api, "getNodeDebugCapabilities").mockResolvedValue(capabilities);
  restoreApi = () => {
    stateSpy.mockRestore();
    capabilitiesSpy.mockRestore();
  };
}

interface ProbeProps {
  apiPort: number;
  workspaceId: string | null;
  sessionId: string | null;
  threadId: string;
  ownerKey: string;
  enabled: boolean;
  mutationGate: NodeDebugMutationGate;
}

let latest: NodeDebugStateSync;

function Probe(props: ProbeProps): React.ReactNode {
  latest = useNodeDebugStateSync(props);
  return null;
}

function props(
  mutationGate: NodeDebugMutationGate,
  overrides: Partial<ProbeProps> = {},
): ProbeProps {
  return {
    apiPort: 49_411,
    workspaceId: "workspace-state-sync",
    sessionId: "session-state-sync",
    threadId: "thread-a",
    ownerKey: "owner-a",
    enabled: false,
    mutationGate,
    ...overrides,
  };
}

afterEach(() => {
  act(() => renderer?.unmount());
  renderer = undefined;
  restoreApi();
  restoreGlobalDescriptor("window", originalWindowDescriptor);
  restoreGlobalDescriptor("BroadcastChannel", originalBroadcastChannelDescriptor);
});

describe("useNodeDebugStateSync", () => {
  test("并发 refresh 与初始轮询共享同一个状态请求", async () => {
    installBrowserGlobals();
    const pending: Deferred<NodeDebugState>[] = [];
    installApiSpies(pending);
    const gate = new NodeDebugMutationGate("owner-a");

    await act(async () => {
      renderer = create(<Probe {...props(gate)} />);
      await Promise.resolve();
    });
    await act(async () => {
      renderer!.update(<Probe {...props(gate, { enabled: true })} />);
      await Promise.resolve();
    });
    expect(pending).toHaveLength(1);

    let firstRefresh!: Promise<void>;
    let secondRefresh!: Promise<void>;
    await act(async () => {
      firstRefresh = latest.refresh();
      secondRefresh = latest.refresh();
      await Promise.resolve();
    });
    expect(pending).toHaveLength(1);

    pending[0].resolve(state("thread-a"));
    await act(async () => {
      await Promise.all([firstRefresh, secondRefresh]);
    });
    expect(latest.state?.thread_id).toBe("thread-a");
  });

  test("owner 切换后迟到的旧轮询响应不能覆盖新 owner", async () => {
    installBrowserGlobals();
    const pending: Deferred<NodeDebugState>[] = [];
    installApiSpies(pending);
    const gate = new NodeDebugMutationGate("owner-a");

    await act(async () => {
      renderer = create(<Probe {...props(gate, { enabled: true })} />);
      await Promise.resolve();
    });
    expect(pending).toHaveLength(1);

    await act(async () => {
      renderer!.update(<Probe {...props(gate, {
        enabled: true,
        ownerKey: "owner-b",
        threadId: "thread-b",
      })} />);
      await Promise.resolve();
    });
    expect(pending).toHaveLength(2);

    pending[1].resolve(state("thread-b", "running"));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(latest.state?.thread_id).toBe("thread-b");

    pending[0].resolve(state("thread-a", "failed"));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(latest.state?.thread_id).toBe("thread-b");
    expect(latest.state?.status).toBe("running");
  });

  test("mutation 失败后强制读取后端权威状态", async () => {
    installBrowserGlobals();
    const pending: Deferred<NodeDebugState>[] = [];
    installApiSpies(pending);
    const gate = new NodeDebugMutationGate("owner-a");
    const mutation = gate.beginMutation("owner-a", "action");
    if (!mutation) throw new Error("测试需要一个可验证的 mutation");

    await act(async () => {
      renderer = create(<Probe {...props(gate)} />);
      await Promise.resolve();
    });

    let refreshPromise!: Promise<void>;
    await act(async () => {
      refreshPromise = latest.refreshAfterMutationFailure("动作失败", mutation);
      await Promise.resolve();
    });
    expect(pending).toHaveLength(1);

    const authoritative = state("thread-a", "paused");
    pending[0].resolve(authoritative);
    await act(async () => {
      await refreshPromise;
    });
    expect(latest.state).toEqual(authoritative);
  });
});
