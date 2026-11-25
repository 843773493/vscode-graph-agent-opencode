import { t } from "../../../../shared/core/i18n/index.js";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { loadSessionHistory } from "../../api/session/sessionTurnHistory";
import type { LoadTurnDetails } from "../../components/chat/turn/types";
import { MessageSubmissionRejectedError, MessageSubmissionUnknownError, reconcileMessageSubmissions, sendUserMessage } from "../../api/session/sessionMessages";
import type { ResourceThreadRef } from "../../api/workspaceResources";
import type { ChildThreadSummary, MessageRunAccepted, TurnHistoryPage } from "../../types/backend";
import { compactInternalExecutionBodies } from "../../state/session/turnBodyRetention";
import { mergeInternalExecutions, type ExecutionItemReference } from "../../state/session/internalExecutionHistory";
import type { ChildThreadHistoryPage } from "../../state/childThreadDisplay";
import { mergeChildThreadHistory } from "../../state/childThreadDisplay";
import { errorMessage } from "../../utils/errorMessage";

export interface ChildThreadConversationSnapshot {
  history: ChildThreadHistoryPage | null;
  report: ChildThreadHistoryPage | null;
  bodyWindow?: Partial<Record<"history" | "report", string[]>>;
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
const INCLUDE = ["user", "metadata", "assistant_text", "final_response", "internal"] as const;
const ITEM_DETAILS = ["text", "reasoning_detail", "reasoning_summary", "encrypted_reasoning_meta", "tool_call", "tool_result", "internal"] as const;

// 导航列表与 Thread 内来源报告共享已读展示态；最后一个视图释放时清空，不增加持久化。
let readReports: Readonly<Record<string, string>> = {};
const reportReadListeners = new Set<() => void>();
const reportReadSnapshot = () => readReports;
function subscribeReportReads(listener: () => void) {
  reportReadListeners.add(listener);
  return () => {
    reportReadListeners.delete(listener);
    if (reportReadListeners.size === 0) readReports = {};
  };
}

function retainPageBodies(page: ChildThreadHistoryPage, retainedIds?: string[]): ChildThreadHistoryPage {
  return retainedIds ? { ...page,
    internal_executions: compactInternalExecutionBodies(page.internal_executions ?? [], retainedIds),
  } : page;
}

function sameHistoryRevision(previous: TurnHistoryPage | null, next: TurnHistoryPage): boolean {
  return previous !== null
    && typeof previous.source_view_id === "string"
    && previous.source_view_id === next.source_view_id
    && typeof previous.source_view_revision === "number"
    && previous.source_view_revision === next.source_view_revision
    && previous.thread_id === next.thread_id
    && previous.projection_epoch === next.projection_epoch;
}

export interface ChildThreadSelectionIntent {
  workspaceId: string;
  sessionId: string;
  threadId: string;
  view: "history" | "report";
  reportExecutionId?: string;
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
  const [selection, setSelection] = useState<(Omit<ChildThreadSelectionIntent, "workspaceId" | "sessionId"> & { scope: string }) | null>(null);
  const [snapshots, setSnapshots] = useState<Record<string, ChildThreadConversationSnapshot>>({});
  const seen = useSyncExternalStore(subscribeReportReads, reportReadSnapshot, reportReadSnapshot);
  const readRef = useRef<AbortController | null>(null);
  const sendingRef = useRef(new Set<string>());
  const selectionIntent: ChildThreadSelectionIntent | null = selection?.scope === scope && workspaceId && sessionId
    ? { workspaceId, sessionId, threadId: selection.threadId, view: selection.view,
      ...(selection.reportExecutionId ? { reportExecutionId: selection.reportExecutionId } : {}) } : null;
  const selectedThread = selectionIntent
    ? threads.find((thread) => thread.thread_id === selectionIntent.threadId) ?? null : null;
  const threadId = selectedThread?.thread_id ?? null;
  const reportExecutionId = selectionIntent?.reportExecutionId ?? selectedThread?.report_execution_id ?? undefined;
  const key = threadId ? JSON.stringify([scope, threadId]) : null;
  const selectionToken = JSON.stringify([key, selectionIntent?.view, selectionIntent?.reportExecutionId]);
  const selectionTokenRef = useRef(selectionToken);
  selectionTokenRef.current = selectionToken;
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
    if (view === "report" && !reportExecutionId) return;
    const previous = latestRef.current;
    const cursor = older ? previous[view]?.before_cursor : null;
    if (older && !cursor) return;
    readRef.current?.abort();
    const controller = new AbortController();
    readRef.current = controller;
    const current = latestRef.current;
    if (!silent || current.view !== view || current.loading || current.error !== null) {
      update(key, (prior) => ({ ...prior, view, loading: !silent, error: null }));
    }
    try {
      const checkRevision = silent && view === "history" && previous.history !== null && previous.error === null;
      let page = await loadSessionHistory(apiPort, sessionId, {
        direction: older ? "before" : "tail",
        cursor: cursor ?? undefined,
        ...(view === "report" ? { execution_id: reportExecutionId } : { turns: checkRevision ? 1 : 10 }),
        include: checkRevision ? ["metadata"] : [...INCLUDE],
      }, workspaceId, controller.signal, threadId);
      if (controller.signal.aborted || readRef.current !== controller || selectionTokenRef.current !== selectionToken) return;
      // page 属于请求的真实容器；继承的历史成员保留其原 Session/Thread scope。
      if (page.thread_id !== threadId) {
        throw new Error(t("child.historyOwnerMismatch"));
      }
      if (checkRevision) {
        // revision 绑定整个 Thread 的历史快照；轮询不重读尚未变化的正文。
        if (sameHistoryRevision(previous.history, page)) return;
        page = await loadSessionHistory(apiPort, sessionId, {
          direction: "tail", turns: 10, include: [...INCLUDE],
        }, workspaceId, controller.signal, threadId);
        if (controller.signal.aborted || readRef.current !== controller || selectionTokenRef.current !== selectionToken) return;
        if (page.thread_id !== threadId) throw new Error(t("child.historyOwnerMismatch"));
      }
      if (view === "report" && !(page.internal_executions ?? []).some((execution) => execution.execution_id === reportExecutionId)) {
        throw new Error(t("child.reportMissing"));
      }
      const current = latestRef.current;
      if (
        older
        || current.loading
        || current.error !== null
        || current.view !== view
        || current.paged
        || !sameHistoryRevision(current[view], page)
      ) {
        update(key, (prior) => ({
          ...prior, [view]: retainPageBodies(mergeChildThreadHistory(older ? prior[view] : null, page), prior.bodyWindow?.[view]),
          view, loading: false, error: null, paged: view === "history" && older,
        }));
      }
      if (view === "report" && reportExecutionId === selectedThread?.report_execution_id && selectedThread?.report_status) {
        const reportKey = JSON.stringify([apiPort, scope, threadId]);
        const revision = `${selectedThread.report_execution_id}:${selectedThread.report_status}`;
        if (readReports[reportKey] !== revision) {
          readReports = { ...readReports, [reportKey]: revision };
          for (const listener of reportReadListeners) listener();
        }
      }
    } catch (error) {
      if (controller.signal.aborted || readRef.current !== controller || selectionTokenRef.current !== selectionToken) return;
      update(key, (prior) => ({ ...prior, loading: false, error: errorMessage(error) }));
    }
  }, [apiPort, key, scope, reportExecutionId, selectionToken, selectedThread?.report_execution_id, selectedThread?.report_status, sessionId, threadId, update, workspaceId]);

  useEffect(() => {
    readRef.current?.abort();
    if (enabled && threadId) void read(selection?.view ?? "history");
    return () => readRef.current?.abort();
  }, [enabled, threadId, scope, read, selection?.view]);

  useEffect(() => {
    if (!enabled || !workspaceId || !sessionId || !threadId || !key) return;
    const controller = new AbortController();
    void reconcileMessageSubmissions(apiPort, { workspaceKey: workspaceId, sessionId, threadId }, controller.signal)
      .then(results => {
        if (!controller.signal.aborted && results.some(result => result.accepted)) void read("history");
      }).catch(error => {
        if (!controller.signal.aborted) update(key, prior => ({ ...prior, error: errorMessage(error) }));
      });
    return () => controller.abort();
  }, [apiPort, enabled, key, read, sessionId, threadId, update, workspaceId]);

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

  const bodyController = useMemo(() => new AbortController(), [apiPort, key, selectionToken, snapshot.view]);
  useEffect(() => () => bodyController.abort(), [bodyController]);
  const bodyLoads = useRef(new Map<string, Promise<void>>());
  const executionBodyWindow = useRef<readonly string[]>([]);
  const retainExecutionBodies = useCallback(async (_turnIds: string[], items: ExecutionItemReference[] = []) => {
    if (!key || !sessionId || !threadId || bodyController.signal.aborted) return;
    const view = latestRef.current.view;
    const token = selectionTokenRef.current;
    const page = latestRef.current[view];
    const retainedIds = items.map(item => item.itemId);
    executionBodyWindow.current = retainedIds;
    update(key, prior => ({ ...prior, [view]: prior[view] ? retainPageBodies(prior[view], retainedIds) : null,
      bodyWindow: { ...prior.bodyWindow, [view]: retainedIds } }));
    const missing = new Map<string, string[]>();
    for (const item of items) {
      const execution = page?.internal_executions?.find(execution => execution.execution_id === item.executionId);
      if (!execution?.display_items.some(part => part.item_id === item.itemId && part.bodyEvicted)
        && !execution?.response_items.some(part => part.source.item_id === item.itemId && part.bodyEvicted)) continue;
      const ids = missing.get(item.executionId) ?? [];
      if (!ids.includes(item.itemId)) ids.push(item.itemId);
      missing.set(item.executionId, ids);
    }
    for (const [executionId, ids] of missing) for (let index = 0; index < ids.length; index += 4) {
      if (bodyController.signal.aborted) return;
      const selected = ids.slice(index, index + 4).filter(id => executionBodyWindow.current.includes(id));
      if (!selected.length) continue;
      const identity = JSON.stringify([token, view, executionId, selected]);
      let pending = bodyLoads.current.get(identity);
      if (!pending) {
        pending = (async () => {
          const response = await loadSessionHistory(apiPort, sessionId, { execution_id: executionId,
            source_view_id: page?.source_view_id ?? undefined, source_view_revision: page?.source_view_revision ?? undefined,
            item_ids: selected, include: [...ITEM_DETAILS] }, workspaceId, bodyController.signal, threadId);
          if (bodyController.signal.aborted || selectionTokenRef.current !== token) return;
          update(key, prior => {
            const current = prior[view];
            if (!current) return prior;
            if (!sameHistoryRevision(current, response)) return { ...prior,
              error: "历史视图已更新，正文补读未应用，请刷新历史或重新打开报告" };
            return { ...prior, [view]: retainPageBodies({ ...current,
              internal_executions: mergeInternalExecutions(current.internal_executions ?? [], response.internal_executions ?? [], response),
            }, prior.bodyWindow?.[view]) };
          });
        })();
        bodyLoads.current.set(identity, pending);
      }
      try { await pending; }
      catch (error) { if (!bodyController.signal.aborted) throw error; }
      finally { bodyLoads.current.delete(identity); }
    }
  }, [apiPort, bodyController, key, sessionId, threadId, update, workspaceId]);
  const reportHistoryError = useCallback((error: unknown) => {
    if (key) update(key, prior => ({ ...prior, error: errorMessage(error) }));
  }, [key, update]);

  const send = useCallback(async () => {
    if (!sessionId || !threadId || !key || sendingRef.current.has(key)) return;
    const content = latestRef.current.draft.trim();
    if (!content) return;
    let failure: string | null = null;
    sendingRef.current.add(key);
    update(key, (prior) => ({ ...prior, sending: true, error: null }));
    try {
      const accepted = await sendUserMessage(apiPort, sessionId, content, "default", [], workspaceId, "after_turn", threadId);
      if (accepted.thread_id !== threadId) throw new Error(t("child.messageOwnerMismatch"));
      update(key, (prior) => ({ ...prior, accepted, draft: prior.draft.trim() === content ? "" : prior.draft }));
    } catch (error) {
      failure = errorMessage(error);
      update(key, (prior) => ({ ...prior, error: failure,
        // 未确认输入已独立冻结；清掉原文本，下一次发送不能误用新身份重复提交。
        draft: (error instanceof MessageSubmissionUnknownError || error instanceof MessageSubmissionRejectedError)
          && prior.draft.trim() === content ? "" : prior.draft }));
    } finally {
      // key 冻结原工作区/Session/child；切换视图后仍须结算原请求，不能留下发送中。
      sendingRef.current.delete(key);
      update(key, (prior) => ({ ...prior, sending: false }));
    }
    if (activeKeyRef.current !== key) return;
    await read("history");
    if (failure !== null) update(key, (prior) => ({ ...prior, error: prior.error ? t("child.refreshFailed", failure, prior.error) : failure }));
  }, [apiPort, key, read, sessionId, threadId, update, workspaceId]);

  const selectThreadForOwner = useCallback((
    owner: { workspaceId: string; sessionId: string },
    id: string,
    view: "history" | "report" = "history",
    reportExecutionId?: string,
  ) => {
    const ownerScope = JSON.stringify([owner.workspaceId, owner.sessionId]);
    setSelection((prior) => prior?.scope === ownerScope && prior.threadId === id && prior.view === view
      && prior.reportExecutionId === reportExecutionId
      ? prior : { scope: ownerScope, threadId: id, view, ...(reportExecutionId ? { reportExecutionId } : {}) });
  }, []);
  const selectThread = useCallback((id: string, view: "history" | "report" = "history") => {
    if (!workspaceId || !sessionId) return;
    selectThreadForOwner({ workspaceId, sessionId }, id, view);
  }, [selectThreadForOwner, sessionId, workspaceId]);
  const close = useCallback(() => {
    readRef.current?.abort();
    setSelection(null);
  }, []);

  const loadProcessDetails = useCallback<LoadTurnDetails>(async (turnIds, _identity, _refresh, include, _tools, selection) => {
    if (!selection || !key || !sessionId || !threadId || !workspaceId) throw new Error("子 Thread 详情缺少精确 owner");
    const token = selectionTokenRef.current;
    const view = latestRef.current.view;
    const response = await loadSessionHistory(apiPort, sessionId, {
      direction: selection.beforeCursor ? "before" : "tail",
      ...(selection.executionId ? {execution_id: selection.executionId} : {turn_ids: turnIds}),
      ...(selection.itemIds ? {item_ids: selection.itemIds} : {}),
      ...(selection.beforeCursor ? {cursor: selection.beforeCursor} : {}),
      include, source_view_id: selection.sourceViewId, source_view_revision: selection.sourceViewRevision,
    }, workspaceId, readRef.current?.signal, threadId);
    if (selectionTokenRef.current !== token) return;
    update(key, prior => ({...prior, [view]: mergeChildThreadHistory(prior[view], response, true, include?.includes("activity_outline"))}));
  }, [apiPort, key, sessionId, threadId, update, workspaceId]);

  return {
    selectedThread, selectionIntent, snapshot, retainExecutionBodies, reportHistoryError, loadProcessDetails,
    owner: sessionId && threadId ? { sessionId, threadId } : null,
    submissionRecovery: enabled && workspaceId && sessionId && threadId
      ? { port: apiPort, owner: { workspaceKey: workspaceId, sessionId, threadId } } : null,
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
      && seen[JSON.stringify([apiPort, scope, thread.thread_id])] !== `${thread.report_execution_id}:${thread.report_status}`),
  };
}
