import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { useState } from "react";
import type { AppState } from "../../types/frontend";
import * as pending from "../../pendingRequestsApi";
import { INITIAL_APP_STATE } from "../app/appStateSeed";
import { cloneMaps } from "../../state/appStateMaps";
import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { installGatewayTestWindow, installVisibleTestDocument } from "../../tests/fixtures/session/gatewayBrowserGlobals";
import { restoreGatewayTestEnvironment } from "../../tests/fixtures/session/gatewayEnvironment";
import { useSessionExecutionSnapshot } from "./useSessionExecutionSnapshot";

const workspace = gatewayWorkspaceKey({ gatewayId: "gateway-one", workspaceId: "workspace-one" });
let renderer: ReactTestRenderer;
let state = INITIAL_APP_STATE;
let controller: ReturnType<typeof useSessionExecutionSnapshot>;
const restores: Array<() => void> = [];
function Probe({ thread = "main", active = true }: { thread?: string; active?: boolean }) {
  const [view, setState] = useState<AppState>(() => ({ ...cloneMaps(INITIAL_APP_STATE), currentSessionWorkspaceId: workspace,
    currentSession: { session_id: "session", thread_id: "main", workspace_id: "workspace-one", resource_identity: "session:one", uri: "boxteam://workspace/workspace-one/resources/session/session-one", scope: "workspace", title: "会话", current_agent_id: "default",
      created_at: "2026-11-15T00:00:00Z", updated_at: "2026-11-15T00:00:00Z" } }));
  state = view;
  controller = useSessionExecutionSnapshot({ active, apiPort: 8111, currentSessionId: "session", currentThreadId: thread,
    currentSessionCacheKey: JSON.stringify([workspace, "session", thread]), currentWorkspaceId: workspace, setState });
  return null;
}
beforeEach(() => { installGatewayTestWindow(8111); installVisibleTestDocument(); });
afterEach(() => { act(() => renderer?.unmount()); restores.splice(0).forEach(restore => restore()); restoreGatewayTestEnvironment(); });

test("执行摘要初始化及重新订阅时确认；请求始终明确Thread，不依赖Goal轮询", async () => {
  const read = spyOn(pending, "listThreadPendingRequests").mockImplementation(async (_p, session, _w, _s, thread) => ({
    session_id: session, thread_id: thread!, active_job_id: `job-${thread}`, requests: [], snapshot_version: 1,
  }));
  const interval = spyOn(window, "setInterval");
  restores.push(() => read.mockRestore(), () => interval.mockRestore());
  await act(async () => { renderer = create(<Probe thread="child" />); });
  expect(read).toHaveBeenCalledTimes(1);
  expect(read.mock.calls[0]?.[4]).toBe("child");
  expect(state.activeJobIdsBySession.get(JSON.stringify([workspace, "session", "child"]))).toBe("job-child");
  await act(async () => { await controller.confirmSessionState(true); });
  expect(read).toHaveBeenCalledTimes(2);
  expect(interval).not.toHaveBeenCalled();
});

test("隐藏取消旧执行读取，恢复时再读，迟到响应不能发布", async () => {
  let visibility!: () => void;
  document.addEventListener = ((name: string, listener: EventListenerOrEventListenerObject) => {
    if (name === "visibilitychange") visibility = listener as () => void;
  }) as typeof document.addEventListener;
  let finish!: (snapshot: Awaited<ReturnType<typeof pending.listThreadPendingRequests>>) => void;
  const read = spyOn(pending, "listThreadPendingRequests").mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  restores.push(() => read.mockRestore());
  await act(async () => { renderer = create(<Probe />); });
  const signal = read.mock.calls[0]?.[3];
  Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
  await act(async () => {
    visibility(); finish({ session_id: "session", thread_id: "main", active_job_id: "late", requests: [], snapshot_version: 2 });
  });
  expect(signal?.aborted).toBe(true);
  expect(state.activeJobIdsBySession.size).toBe(0);
  Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
  await act(async () => { visibility(); });
  expect(read).toHaveBeenCalledTimes(2);
});
