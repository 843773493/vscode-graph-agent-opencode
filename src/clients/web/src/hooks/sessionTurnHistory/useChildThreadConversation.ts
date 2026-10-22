import { useCallback, useEffect, useRef, useState } from "react";
import { loadSessionHistory } from "../../api/session/sessionTurnHistory";
import { sendUserMessage } from "../../api/session/sessionMessages";
import type { ResourceThreadRef } from "../../api/workspaceResources";
import type { ChildThreadSummary, MessageRunAccepted, TurnHistoryPage } from "../../types/backend";
import { mergeChildThreadHistory } from "../../state/childThreadDisplay";
import { errorMessage } from "../../utils/errorMessage";

export interface ChildThreadConversationSnapshot {
  history: TurnHistoryPage | null;
  report: TurnHistoryPage | null;
  view: "history" | "report";
  draft: string;
  loading: boolean;
  sending: boolean;
  error: string | null;
  accepted: MessageRunAccepted | null;
  paged: boolean;
}

const EMPTY: ChildThreadConversationSnapshot = {
  history: null, report: null, view: "history", draft: "", loading: false,
  sending: false, error: null, accepted: null, paged: false,
};
const INCLUDE = ["user", "text", "reasoning_detail", "encrypted_reasoning_meta", "tool_call", "tool_result", "final_response", "internal"] as const;

export interface ChildThreadSelectionIntent {
  workspaceId: string;
  sessionId: string;
  threadId: string;
  view: "history" | "report";
}

export function resolveTerminalResourceTarget({
  childPanelVisible,
  selectionIntent,
  selectedThread,
  bottomPanelWorkspaceId,
  mainThread,
}: {
  childPanelVisible: boolean;
  selectionIntent: ChildThreadSelectionIntent | null;
  selectedThread: ChildThreadSummary | null;
  bottomPanelWorkspaceId: string | null;
  mainThread: ResourceThreadRef | null;
}): { thread: ResourceThreadRef | null; unavailable: boolean } {
  if (!childPanelVisible || !selectionIntent) {
    return { thread: mainThread, unavailable: false };
  }
  if (
    selectionIntent.workspaceId !== bottomPanelWorkspaceId
    || selectedThread?.thread_id !== selectionIntent.threadId
  ) {
    return { thread: null, unavailable: true };
  }
  return {
    thread: { session_id: selectionIntent.sessionId, thread_id: selectionIntent.threadId },
    unavailable: false,
  };
}

/** 子线程对话只读取精确 owner 的冷历史；缓存与草稿属于展示态。 */
export function useChildThreadConversation({
  apiPort, workspaceId, sessionId, threads, enabled,
}: {
  apiPort: number; workspaceId: string | null; sessionId: string | null;
  threads: ChildThreadSummary[]; enabled: boolean;
}) {
  const scope = JSON.stringify([workspaceId, sessionId]);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const [selection, setSelection] = useState<{ scope: string; threadId: string; view: "history" | "report" } | null>(null);
  const [snapshots, setSnapshots] = useState<Record<string, ChildThreadConversationSnapshot>>({});
  const [seen, setSeen] = useState<Record<string, string>>({});
  const readRef = useRef<AbortController | null>(null);
  const sendingRef = useRef(new Set<string>());
  const selectionIntent: ChildThreadSelectionIntent | null = selection?.scope === scope && workspaceId && sessionId
    ? { workspaceId, sessionId, threadId: selection.threadId, view: selection.view } : null;
  const selectedThread = selectionIntent
    ? threads.find((thread) => thread.thread_id === selectionIntent.threadId) ?? null : null;
  const threadId = selectedThread?.thread_id ?? null;
  const key = threadId ? JSON.stringify([scope, threadId]) : null;
  const activeKeyRef = useRef(key);
  activeKeyRef.current = key;
  const snapshot = key ? snapshots[key] ?? EMPTY : EMPTY;
  const latestRef = useRef(snapshot);
  latestRef.current = snapshot;

  const update = useCallback((targetKey: string, transform: (prior: ChildThreadConversationSnapshot) => ChildThreadConversationSnapshot) => {
    setSnapshots((prior) => ({ ...prior, [targetKey]: transform(prior[targetKey] ?? EMPTY) }));
  }, []);

  const read = useCallback(async (view: "history" | "report", older = false, silent = false) => {
    if (!sessionId || !threadId || !key || activeKeyRef.current !== key) return;
    if (view === "report" && !selectedThread?.report_execution_id) return;
    const ownerScope = scope;
    const previous = latestRef.current;
    const cursor = older ? previous[view]?.before_cursor : null;
    if (older && !cursor) return;
    readRef.current?.abort();
    const controller = new AbortController();
    readRef.current = controller;
    update(key, (prior) => ({ ...prior, view, loading: !silent, error: null }));
    try {
      const page = await loadSessionHistory(apiPort, sessionId, {
        direction: older ? "before" : "tail",
        cursor: cursor ?? undefined,
        ...(view === "report" ? { execution_id: selectedThread?.report_execution_id ?? undefined } : { turns: 10 }),
        include: [...INCLUDE],
      }, workspaceId, controller.signal, threadId);
      if (controller.signal.aborted || scopeRef.current !== ownerScope) return;
      // page 属于请求的真实容器；继承的历史成员保留其原 Session/Thread scope。
      if (page.thread_id !== threadId) {
        throw new Error("子线程历史响应的 owner 与请求不一致");
      }
      if (view === "report" && !(page.internal_executions ?? []).some((execution) => execution.execution_id === selectedThread?.report_execution_id)) {
        throw new Error("该报告尚未提交或已不在当前历史视图中");
      }
      update(key, (prior) => ({
        ...prior, [view]: older ? mergeChildThreadHistory(prior[view], page) : page,
        view, loading: false, error: null, paged: view === "history" && older,
      }));
      if (view === "report" && selectedThread?.report_execution_id && selectedThread.report_status) {
        setSeen((prior) => ({ ...prior, [key]: `${selectedThread.report_execution_id}:${selectedThread.report_status}` }));
      }
    } catch (error) {
      if (controller.signal.aborted || scopeRef.current !== ownerScope) return;
      update(key, (prior) => ({ ...prior, loading: false, error: errorMessage(error) }));
    }
  }, [apiPort, key, scope, selectedThread?.report_execution_id, selectedThread?.report_status, sessionId, threadId, update, workspaceId]);

  useEffect(() => {
    readRef.current?.abort();
    if (enabled && threadId) void read(selection?.view ?? "history");
    return () => readRef.current?.abort();
  }, [enabled, threadId, scope, read, selection?.view]);

  useEffect(() => {
    if (!enabled || !threadId) return;
    let polling = false;
    const timer = globalThis.setInterval(() => {
      if (polling || latestRef.current.loading || latestRef.current.paged || latestRef.current.view !== "history"
        || (typeof document !== "undefined" && document.visibilityState !== "visible")) return;
      polling = true;
      void read("history", false, true).finally(() => { polling = false; });
    }, 2500);
    return () => globalThis.clearInterval(timer);
  }, [enabled, read, threadId]);

  const send = useCallback(async () => {
    if (!sessionId || !threadId || !key || sendingRef.current.has(key)) return;
    const content = latestRef.current.draft.trim();
    if (!content) return;
    let failure: string | null = null;
    sendingRef.current.add(key);
    update(key, (prior) => ({ ...prior, sending: true, error: null }));
    try {
      const accepted = await sendUserMessage(apiPort, sessionId, content, "default", [], workspaceId, "after_turn", threadId);
      if (accepted.thread_id !== threadId) throw new Error("子线程消息响应的 owner 与请求不一致");
      update(key, (prior) => ({ ...prior, accepted, draft: prior.draft.trim() === content ? "" : prior.draft }));
    } catch (error) {
      failure = errorMessage(error);
      update(key, (prior) => ({ ...prior, error: failure }));
    } finally {
      // key 冻结原工作区/Session/child；切换视图后仍须结算原请求，不能留下发送中。
      sendingRef.current.delete(key);
      update(key, (prior) => ({ ...prior, sending: false }));
    }
    if (activeKeyRef.current !== key) return;
    await read("history");
    if (failure !== null) update(key, (prior) => ({ ...prior, error: prior.error ? `${failure}；重新读取失败：${prior.error}` : failure }));
  }, [apiPort, key, read, sessionId, threadId, update, workspaceId]);

  const selectThreadForOwner = useCallback((
    owner: { workspaceId: string; sessionId: string },
    id: string,
    view: "history" | "report" = "history",
  ) => {
    const ownerScope = JSON.stringify([owner.workspaceId, owner.sessionId]);
    setSelection((prior) => prior?.scope === ownerScope && prior.threadId === id && prior.view === view
      ? prior : { scope: ownerScope, threadId: id, view });
  }, []);
  const selectThread = useCallback((id: string, view: "history" | "report" = "history") => {
    if (!workspaceId || !sessionId) return;
    selectThreadForOwner({ workspaceId, sessionId }, id, view);
  }, [selectThreadForOwner, sessionId, workspaceId]);
  const close = useCallback(() => {
    readRef.current?.abort();
    setSelection(null);
  }, []);

  return {
    selectedThread, selectionIntent, snapshot,
    selectThread,
    selectThreadForOwner,
    close,
    setDraft: (draft: string) => { if (key) update(key, (prior) => ({ ...prior, draft })); },
    refresh: () => read("history"),
    openReport: () => read("report"),
    loadEarlier: () => read(snapshot.view, true),
    send,
    isUnread: (thread: ChildThreadSummary) => Boolean(thread.report_execution_id && thread.report_status
      && thread.report_status !== "unknown"
      && seen[JSON.stringify([scope, thread.thread_id])] !== `${thread.report_execution_id}:${thread.report_status}`),
  };
}
