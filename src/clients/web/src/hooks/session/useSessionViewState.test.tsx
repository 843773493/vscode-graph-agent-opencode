import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as gatewayApi from "../../gatewayApi";
import type {
  GatewayUserAccess,
  GatewayUserViewState,
} from "../../types/backend";
import {
  useSessionViewState,
  type SessionViewStateController,
  type SessionViewStateHost,
} from "./useSessionViewState";

function viewState(overrides: Partial<GatewayUserViewState> = {}): GatewayUserViewState {
  return {
    user_id: "user-view-state",
    workspace_id: "workspace-view-state",
    session_id: "session-view-state",
    turn_anchor: "turn-0",
    scroll_offset: 0,
    follow_latest: true,
    projection_version: 1,
    tool_details_expanded: false,
    updated_at: "2026-09-20T00:00:00Z",
    ...overrides,
  };
}

function access(leaseGeneration = 3): GatewayUserAccess {
  return {
    kind: "user",
    user_id: "user-view-state",
    lease_generation: leaseGeneration,
    expires_at: "2026-09-20T00:00:00Z",
    takeover: false,
  };
}

const host: SessionViewStateHost = {
  apiPort: 49_413,
  currentWorkspaceId: "workspace-view-state",
  currentSessionId: "session-view-state",
  gatewayUserAccess: access(),
  gatewayUserViewStates: new Map(),
  expandDetails: false,
};

const originalFetch = globalThis.fetch;
let renderer: ReactTestRenderer | undefined;
let restoreApi = () => {};

afterEach(() => {
  act(() => renderer?.unmount());
  renderer = undefined;
  globalThis.fetch = originalFetch;
  restoreApi();
});

describe("useSessionViewState", () => {
  test("并发加载共享请求，并在成功后完整应用后端对象", async () => {
    let resolveRequest!: (value: GatewayUserViewState) => void;
    let requests = 0;
    const reader = spyOn(gatewayApi, "getGatewayUserViewState").mockImplementation(
      async () => {
        requests += 1;
        return await new Promise<GatewayUserViewState>((resolve) => {
          resolveRequest = resolve;
        });
      },
    );
    restoreApi = () => reader.mockRestore();

    let controller!: SessionViewStateController;
    const applied: Array<{ viewState: GatewayUserViewState | null; expanded?: boolean }> = [];
    function Probe(): React.ReactNode {
      controller = useSessionViewState({
        host,
        onApplyViewState: ({ viewState: next, toolDetailsExpanded }) => {
          applied.push({ viewState: next, expanded: toolDetailsExpanded });
        },
        onSetExpandDetails: () => undefined,
        onStatusChange: () => undefined,
      });
      return null;
    }

    await act(async () => { renderer = create(<Probe />); });
    const first = controller.loadSessionViewState("workspace-view-state", "session-view-state");
    const second = controller.loadSessionViewState("workspace-view-state", "session-view-state");
    await Promise.resolve();
    expect(requests).toBe(1);

    const loaded = viewState({ tool_details_expanded: true, scroll_offset: 48 });
    resolveRequest(loaded);
    await act(async () => {
      await Promise.all([first, second]);
    });
    expect(applied).toEqual([{ viewState: loaded, expanded: true }]);
  });

  test("保存响应在 lease generation 变化后不污染当前用户状态", async () => {
    let resolveRequest!: (value: GatewayUserViewState) => void;
    const writer = spyOn(gatewayApi, "putGatewayUserViewState").mockImplementation(
      async () => await new Promise<GatewayUserViewState>((resolve) => {
        resolveRequest = resolve;
      }),
    );
    restoreApi = () => writer.mockRestore();

    let controller!: SessionViewStateController;
    const applied: GatewayUserViewState[] = [];
    let currentHost = { ...host, gatewayUserAccess: access(7) };
    function Probe(): React.ReactNode {
      controller = useSessionViewState({
        host: currentHost,
        onApplyViewState: ({ viewState: next }) => { if (next) applied.push(next); },
        onSetExpandDetails: () => undefined,
        onStatusChange: () => undefined,
      });
      return null;
    }

    await act(async () => { renderer = create(<Probe />); });
    controller.saveSessionViewState({
      turn_anchor: "turn-1",
      scroll_offset: 10,
      follow_latest: true,
    });
    await Promise.resolve();
    currentHost = { ...currentHost, gatewayUserAccess: access(8) };
    await act(async () => { renderer!.update(<Probe />); });
    resolveRequest(viewState({ scroll_offset: 10 }));
    await act(async () => { await Promise.resolve(); });
    expect(applied).toHaveLength(0);
  });
});
