import { afterEach, expect, spyOn, test } from "bun:test";
import React, { useState } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as workspaceResourcesApi from "../../api/workspaceResources";
import type { ResourceThreadRef, WorkspaceResourceDTO } from "../../api/workspaceResources";
import type { Session } from "../../types/backend";
import type { AppState } from "../../types/frontend";
import { resolveTerminalResourceTarget, useChildThreadConversation } from "../sessionTurnHistory/useChildThreadConversation";
import { useSessionResourceLoader } from "../session/useSessionResourceLoader";
import { apiResponse, installGatewayFetch, restoreSessionHookGlobals } from "../session/sessionHookTestFixtures";
import { useWorkspaceTerminalResources } from "./useWorkspaceTerminalResources";
import {
  closeChildConversationAndClearForkSelection,
  createTerminalFromResourcePanel,
} from "./createTerminalFromResourcePanel";

const API_PORT = 49_946;
const WORKSPACE_ID = "gateway-terminal-owner";
const SESSION_ID = "ses_terminal_owner";
const MAIN_THREAD: ResourceThreadRef = { session_id: SESSION_ID, thread_id: "thr_main" };
const CHILD_THREAD_ID = "thr_child_selected";
const CURRENT_SESSION = { session_id: SESSION_ID, workspace_id: "backend-owner" } as Session;
const CHILDREN = [{ thread_id: CHILD_THREAD_ID, created_at: "2026-10-05T00:00:00Z", status: "completed" as const }];
const createdTerminal: WorkspaceResourceDTO = {
  resource_id: "term_created_from_resource_panel",
  workspace_id: "backend-owner",
  kind: "terminal",
  retention_scope: "thread",
  source_thread: MAIN_THREAD,
  thread_associations: [MAIN_THREAD],
  owner_revision: 3,
  status: "running",
  reconciliation_status: "verified",
  available_actions: ["promote", "attach", "detach", "stop", "delete"],
  metadata: { title: "用户终端" },
  created_at: "2026-10-05T00:00:00Z",
  updated_at: "2026-10-05T00:00:01Z",
};

let current!: {
  childConversation: ReturnType<typeof useChildThreadConversation>;
  terminalController: ReturnType<typeof useWorkspaceTerminalResources>;
  target: ReturnType<typeof resolveTerminalResourceTarget>;
  childSelectionIntent: ReturnType<typeof useChildThreadConversation>["selectionIntent"];
  panel: { tab: string; terminalId: string | null };
  createFromResourcePanel: () => Promise<WorkspaceResourceDTO>;
};
let renderer: ReactTestRenderer | null = null;
const restores: Array<() => void> = [];

function emptyState(): AppState {
  return {
    currentSession: CURRENT_SESSION,
    currentSessionWorkspaceId: WORKSPACE_ID,
    sessionResources: [],
    sessionResourcesLoadedAt: null,
    sessionResourcesLoading: false,
    sessionResourcesError: null,
    status: "资源视图",
  } as unknown as AppState;
}

function Harness({ pendingForkSelection = null }: { pendingForkSelection?: string | null }) {
  const [, setSessionState] = useState<AppState>(emptyState);
  const [panel, setPanel] = useState({ tab: "resources", terminalId: null as string | null });
  const [forkSelection, setForkSelection] = useState(pendingForkSelection);
  const childConversation = useChildThreadConversation({
    apiPort: API_PORT,
    workspaceId: WORKSPACE_ID,
    sessionId: SESSION_ID,
    threads: CHILDREN,
    enabled: false,
  });
  const terminalController = useWorkspaceTerminalResources(API_PORT, WORKSPACE_ID, false);
  const sessionResources = useSessionResourceLoader({
    apiPort: API_PORT,
    currentSession: CURRENT_SESSION,
    workspaceId: WORKSPACE_ID,
    setState: setSessionState,
  });
  const childSelectionIntent = childConversation.selectionIntent ?? (forkSelection ? {
    workspaceId: WORKSPACE_ID,
    sessionId: SESSION_ID,
    threadId: forkSelection,
    view: "history" as const,
  } : null);
  const target = resolveTerminalResourceTarget({
    childPanelVisible: true,
    selectionIntent: childSelectionIntent,
    selectedThread: childConversation.selectedThread,
    bottomPanelWorkspaceId: WORKSPACE_ID,
    mainThread: MAIN_THREAD,
  });
  current = {
    childConversation,
    terminalController,
    target,
    childSelectionIntent,
    panel,
    createFromResourcePanel: () => createTerminalFromResourcePanel({
      sessionId: SESSION_ID,
      thread: MAIN_THREAD,
      createTerminal: terminalController.create,
      refreshSessionResources: sessionResources.refreshSessionResources,
      closeChildConversation: () => {
        closeChildConversationAndClearForkSelection(
          childConversation.close,
          () => setForkSelection(null),
        );
      },
      openTerminalPanel: (terminalId) => setPanel({ tab: "terminal", terminalId }),
    }),
  };
  return null;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

async function until(condition: () => boolean) {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    if (condition()) return;
    await Promise.resolve();
  }
  throw new Error("终端创建没有进入会话资源刷新阶段");
}

async function mount(pendingForkSelection: string | null = null) {
  await act(async () => { renderer = create(<Harness pendingForkSelection={pendingForkSelection} />); });
  if (!pendingForkSelection) {
    act(() => current.childConversation.selectThread(CHILD_THREAD_ID));
    expect(current.target.thread).toEqual({ session_id: SESSION_ID, thread_id: CHILD_THREAD_ID });
  } else {
    expect(current.target).toEqual({ thread: null, unavailable: true });
  }
  return current;
}

afterEach(async () => {
  if (renderer) await act(async () => renderer?.unmount());
  renderer = null;
  for (const restore of restores.splice(0)) restore();
  restoreSessionHookGlobals();
});

test("主线程终端创建等待强制 session 刷新后收起 child，并让底部 owner 回到 main", async () => {
  const create = spyOn(workspaceResourcesApi, "createWorkspaceTerminal").mockResolvedValue(createdTerminal);
  restores.push(() => create.mockRestore());
  const read = deferred<Response>();
  let resourceReads = 0;
  installGatewayFetch(({ path }) => {
    if (!path.endsWith(`/sessions/${SESSION_ID}/resources`)) return undefined;
    resourceReads += 1;
    return read.promise;
  });
  await mount(CHILD_THREAD_ID);

  let pending!: Promise<WorkspaceResourceDTO>;
  await act(async () => {
    pending = current.createFromResourcePanel();
    await until(() => resourceReads === 1);
    expect(create).toHaveBeenCalledWith(API_PORT, WORKSPACE_ID, MAIN_THREAD, "thread");
    expect(current.childSelectionIntent?.threadId).toBe(CHILD_THREAD_ID);
    expect(current.panel).toEqual({ tab: "resources", terminalId: null });
    read.resolve(apiResponse({ session_id: SESSION_ID, items: [] }));
    await pending;
  });

  expect(current.childConversation.selectionIntent).toBeNull();
  expect(current.childSelectionIntent).toBeNull();
  expect(current.target.thread).toEqual(MAIN_THREAD);
  expect(current.panel).toEqual({ tab: "terminal", terminalId: createdTerminal.resource_id });
  expect(current.terminalController.items).toEqual([createdTerminal]);
  expect(resourceReads).toBe(1);
});

test("会话资源强制刷新失败时保留创建事实、child 选择与原底部面板", async () => {
  const create = spyOn(workspaceResourcesApi, "createWorkspaceTerminal").mockResolvedValue(createdTerminal);
  restores.push(() => create.mockRestore());
  installGatewayFetch(({ path }) => path.endsWith(`/sessions/${SESSION_ID}/resources`)
    ? Response.json({ detail: "session resource owner unavailable" }, { status: 503 })
    : undefined);
  await mount();

  let failure: unknown;
  await act(async () => {
    failure = await current.createFromResourcePanel().catch((cause: unknown) => cause);
  });

  expect(String(failure)).toContain("已创建");
  expect(String(failure)).toContain("会话连接列表刷新失败");
  expect(current.terminalController.items).toEqual([createdTerminal]);
  expect(current.childConversation.selectionIntent?.threadId).toBe(CHILD_THREAD_ID);
  expect(current.target.thread).toEqual({ session_id: SESSION_ID, thread_id: CHILD_THREAD_ID });
  expect(current.panel).toEqual({ tab: "resources", terminalId: null });
});

test("owner 切换发生在资源刷新期间时保留创建事实且不改动新面板", async () => {
  let closed = false;
  const opened: string[] = [];

  await expect(createTerminalFromResourcePanel({
    sessionId: SESSION_ID,
    thread: MAIN_THREAD,
    createTerminal: async () => createdTerminal,
    refreshSessionResources: async () => "stale",
    closeChildConversation: () => { closed = true; },
    openTerminalPanel: (terminalId) => { opened.push(terminalId); },
  })).rejects.toThrow(
    `终端 ${createdTerminal.resource_id} 已创建，但当前会话已切换；子线程仍保持打开，请刷新资源后再打开终端。`,
  );

  expect(closed).toBe(false);
  expect(opened).toEqual([]);
});

test("终端创建失败时不启动 session 刷新、不关闭 child，也不切换底部面板", async () => {
  const create = spyOn(workspaceResourcesApi, "createWorkspaceTerminal").mockRejectedValue(new Error("terminal create conflict"));
  restores.push(() => create.mockRestore());
  const list = spyOn(workspaceResourcesApi, "listWorkspaceTerminals").mockResolvedValue({
    workspace_id: "backend-owner",
    items: [],
  });
  restores.push(() => list.mockRestore());
  let resourceReads = 0;
  installGatewayFetch(({ path }) => {
    if (path.endsWith(`/sessions/${SESSION_ID}/resources`)) resourceReads += 1;
    return undefined;
  });
  await mount();

  let failure: unknown;
  await act(async () => {
    failure = await current.createFromResourcePanel().catch((cause: unknown) => cause);
  });

  expect(String(failure)).toContain("terminal create conflict");
  expect(list).toHaveBeenCalledTimes(1);
  expect(resourceReads).toBe(0);
  expect(current.terminalController.items).toEqual([]);
  expect(current.childConversation.selectionIntent?.threadId).toBe(CHILD_THREAD_ID);
  expect(current.panel).toEqual({ tab: "resources", terminalId: null });
});
