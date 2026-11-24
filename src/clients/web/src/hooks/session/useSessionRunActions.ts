import { useCallback, useEffect } from "react";
import { MessageSubmissionUnknownError, reconcileMessageSubmissions } from "../../api/session/sessionMessages";
import { loadMessageSubmissions, messageSubmissionKey } from "../../state/messageSubmissions/storage";
import { mergeSubmissionConversation, submissionConversation } from "../../state/messageSubmissions/projection";
import { subscribeBrowserViews } from "../../state/browserViewStore";
import { createSnapshotReadCoordinator, SnapshotReadSupersededError } from "../../../../shared/core/syncState";
import {
  compactSessionContext as apiCompactSessionContext,
  DEFAULT_SESSION_TITLE,
  replayMessageTurn as apiReplayMessageTurn,
  sendUserMessage as apiSendMessage,
} from "../../api";
import type {
  AttachmentRef,
  MessageReplayRequest,
  MessageRunAccepted,
  DeliveryPolicy,
  Session,
  SessionCompactResult,
} from "../../types/backend";
import type { AppState, ConversationContentView, ConversationView } from "../../types/frontend";
import { updateSessionAttachmentSummary } from "../../state/attachments";
import {
  dispatchToConversationProjection,
  writeDispatchActiveJob,
} from "../../state/conversations";
import { appendFrontendEvent } from "../../state/traceEvents";
import { errorMessage } from "../../utils/errorMessage";
import type { SetAppState } from "../contentViewLoaderTypes";
import { sessionScopeKey } from "../../state/session/sessionScope";
import type { useSessionLifecycleActions } from "./useSessionLifecycleActions";
import { usePendingRequestActions } from "../runtime/usePendingRequestActions";

function isCurrentSessionOwner(
  state: Pick<
    AppState,
    "currentSession" | "currentSessionWorkspaceId" | "activeGatewayWorkspaceId"
  >,
  sessionId: string,
  workspaceId: string | null,
): boolean {
  return state.currentSession !== null
    && sessionScopeKey(
      state.currentSessionWorkspaceId ?? state.activeGatewayWorkspaceId,
      state.currentSession.session_id,
    ) === sessionScopeKey(workspaceId, sessionId);
}

export function useSessionRunActions({
  apiPort,
  currentSession,
  currentThreadId,
  activeGatewayWorkspaceId,
  currentSessionGatewayWorkspaceId,
  currentSessionCacheKey,
  defaultGatewayWorkspaceId,
  active = true,
  contentView,
  setState,
  refreshAgentStateSnapshot,
  createSession,
}: {
  apiPort: number;
  currentSession: Session | null;
  currentThreadId?: string;
  activeGatewayWorkspaceId: string | null;
  currentSessionGatewayWorkspaceId: string | null;
  currentSessionCacheKey: string | null;
  defaultGatewayWorkspaceId: string | null;
  active?: boolean;
  contentView: ConversationContentView;
  setState: SetAppState;
  refreshAgentStateSnapshot: (sessionId: string) => Promise<void>;
  createSession: ReturnType<typeof useSessionLifecycleActions>["createSession"];
}) {
  const pendingRequestActions = usePendingRequestActions({
    apiPort,
    currentSession,
    currentThreadId,
    currentSessionGatewayWorkspaceId,
    currentSessionCacheKey,
    setState,
  });
  const submissionThread = currentThreadId ?? currentSession?.thread_id;
  const submissionSession = currentSession?.session_id;
  useEffect(() => {
    if (!active || !submissionSession || !submissionThread || !currentSessionGatewayWorkspaceId || !currentSessionCacheKey) return;
    const owner = { workspaceKey: currentSessionGatewayWorkspaceId, sessionId: submissionSession, threadId: submissionThread };
    const storageKey = messageSubmissionKey(owner);
    let observing = true;
    const reads = createSnapshotReadCoordinator();
    const tracked = new Set<string>();
    const canPublish = () => observing && document.visibilityState !== "hidden";
    const apply = (results: Awaited<ReturnType<typeof reconcileMessageSubmissions>>) => {
      if (!canPublish()) return;
      setState(previous => {
        if (!canPublish()) return previous;
        const next = { ...previous, pendingConversations: new Map(previous.pendingConversations),
          activeJobIdsBySession: new Map(previous.activeJobIdsBySession), pendingQueueVersions: new Map(previous.pendingQueueVersions) };
        for (const { record, accepted } of results) {
          const existing = next.pendingConversations.get(currentSessionCacheKey) ?? [];
          next.pendingConversations.set(currentSessionCacheKey, mergeSubmissionConversation(existing, submissionConversation(record, accepted)));
          if (accepted) writeDispatchActiveJob(next.activeJobIdsBySession, accepted.dispatch, currentSessionCacheKey, next.pendingQueueVersions);
        }
        return next;
      });
    };
    const confirm = async () => {
      if (!observing || document.visibilityState === "hidden") return;
      try {
        const results = await reads.read(storageKey, async signal => {
          const records = await loadMessageSubmissions(owner);
          signal.throwIfAborted();
          if (canPublish()) setState(previous => {
            if (!canPublish() || signal.aborted) return previous;
            const pending = previous.pendingConversations.get(currentSessionCacheKey) ?? [];
            const ids = new Set(records.map(record => record.request.operation_id));
            const retained = pending.filter(item => !item.pendingSubmissionId || item.jobId
              || !tracked.has(item.pendingSubmissionId) || ids.has(item.pendingSubmissionId));
            for (const id of ids) tracked.add(id);
            return { ...previous, pendingConversations: new Map(previous.pendingConversations).set(currentSessionCacheKey, retained) };
          });
          apply(records.map(record => ({ record, accepted: null })));
          return reconcileMessageSubmissions(apiPort, owner, signal);
        });
        apply(results);
      } catch (error) {
        if (error instanceof SnapshotReadSupersededError) return;
        if (canPublish()) setState(previous => canPublish() && isCurrentSessionOwner(previous, submissionSession, owner.workspaceKey)
          ? { ...previous, status: `消息接收状态未确认: ${errorMessage(error)}` } : previous);
      }
    };
    const refresh = () => { void confirm(); };
    let timer: ReturnType<typeof globalThis.setInterval> | undefined;
    const visibilityChanged = () => {
      if (document.visibilityState === "hidden") {
        globalThis.clearInterval(timer); timer = undefined;
        reads.invalidate(storageKey);
      } else {
        if (timer === undefined) timer = globalThis.setInterval(refresh, 30_000);
        refresh();
      }
    };
    visibilityChanged();
    window.addEventListener("focus", refresh);
    window.addEventListener("online", refresh);
    document.addEventListener("visibilitychange", visibilityChanged);
    const unsubscribe = subscribeBrowserViews(key => { if (key === storageKey) refresh(); });
    return () => {
      observing = false; reads.dispose(); globalThis.clearInterval(timer); unsubscribe();
      window.removeEventListener("focus", refresh); window.removeEventListener("online", refresh);
      document.removeEventListener("visibilitychange", visibilityChanged);
    };
  }, [active, apiPort, submissionSession, submissionThread, currentSessionGatewayWorkspaceId, currentSessionCacheKey, setState]);
  const sendMessage = useCallback(
    async (
      content: string,
      attachments: AttachmentRef[] = [],
      deliveryPolicy: DeliveryPolicy = "after_turn",
    ) => {
      const activeSession = currentSession ?? await createSession(
        DEFAULT_SESSION_TITLE, activeGatewayWorkspaceId ?? defaultGatewayWorkspaceId,
      );
      const targetThreadId = currentThreadId ?? activeSession.thread_id;
      if (!targetThreadId) throw new Error(`发送消息缺少 catalog 主线程身份: ${activeSession.session_id}`);
      const activeSessionGatewayWorkspaceId =
        currentSessionGatewayWorkspaceId ??
        activeGatewayWorkspaceId ??
        defaultGatewayWorkspaceId;
      const activeSessionCacheKey =
        currentSession && currentSessionCacheKey
          ? currentSessionCacheKey
          : sessionScopeKey(activeSessionGatewayWorkspaceId, activeSession.session_id);
      const pendingSubmissionId = `op_${crypto.randomUUID().replaceAll("-", "")}`;
      const submittedAt = new Date().toISOString();
      setState((prev) => {
        const next = {
          ...prev,
          pendingConversations: new Map(prev.pendingConversations),
          pendingQueueVersions: new Map(prev.pendingQueueVersions),
          sessionAttachmentSummaries: attachments.length > 0
            ? new Map(prev.sessionAttachmentSummaries)
            : prev.sessionAttachmentSummaries,
        };
        const conversation: ConversationView = {
          conversationId: pendingSubmissionId,
          displayMode: "live",
          sessionId: activeSession.session_id,
          threadId: targetThreadId,
          userMessage: {
            message_id: pendingSubmissionId,
            session_id: activeSession.session_id,
            thread_id: targetThreadId,
            role: "user",
            content,
            metadata: {
              source: "optimistic",
              pending_submission_id: pendingSubmissionId,
            },
            attachments,
            created_at: submittedAt,
            updated_at: submittedAt,
          },
          assistantMessages: [],
          events: [],
          status: "queued",
          jobId: null,
          pending: true,
          activeJobOverlay: false,
          pendingSubmissionId,
          source: "pending",
          deliveryPolicy,
        };
        updateSessionAttachmentSummary(
          next.sessionAttachmentSummaries,
          activeSessionCacheKey,
          attachments,
          submittedAt,
        );
        const pendingList =
          next.pendingConversations.get(activeSessionCacheKey) ?? [];
        next.pendingConversations.set(activeSessionCacheKey, [
          ...pendingList,
          conversation,
        ]);
        if (isCurrentSessionOwner(prev, activeSession.session_id, activeSessionGatewayWorkspaceId)) {
          next.status = "正在发送消息";
          next.contentView = prev.contentView === "agent" ? "default" : prev.contentView;
        }
        return next;
      });

      let accepted: MessageRunAccepted;
      try {
        accepted = await apiSendMessage(
          apiPort,
          activeSession.session_id,
          content,
          activeSession.current_agent_id,
          attachments,
          activeSessionGatewayWorkspaceId,
          deliveryPolicy,
          targetThreadId,
          pendingSubmissionId,
        );
        if (currentThreadId !== undefined && accepted.thread_id !== targetThreadId) throw new Error("消息接受结果的 Thread 归属不一致");
      } catch (error) {
        const message = errorMessage(error);
        setState((prev) => {
          const next = {
            ...prev,
            pendingConversations: new Map(prev.pendingConversations),
            pendingQueueVersions: new Map(prev.pendingQueueVersions),
            activeJobIdsBySession: new Map(prev.activeJobIdsBySession),
          };
          const pendingList = next.pendingConversations.get(activeSessionCacheKey) ?? [];
          next.pendingConversations.set(activeSessionCacheKey, pendingList.map(conversation =>
            conversation.pendingSubmissionId === pendingSubmissionId
              ? { ...conversation, status: "error", pending: error instanceof MessageSubmissionUnknownError,
                  userMessage: conversation.userMessage ? { ...conversation.userMessage,
                    metadata: { ...conversation.userMessage.metadata, submission_error: message } } : null }
              : conversation,
          ));
          // 待处理队列按会话作用域写回，切走后仍须校准发起会话自己的队列；
          // 但失败文案是全局状态栏字段，当前 Gateway workspace + Session owner
          // 已切走时不得覆盖。
          if (
            isCurrentSessionOwner(
              prev,
              activeSession.session_id,
              activeSessionGatewayWorkspaceId,
            )
          ) {
            next.status = `发送失败: ${message}`;
          }
          return next;
        });
        throw error;
      }
      const messageId = accepted.message_id;
      const jobId = accepted.job_id;

      setState((prev) => {
        const next = {
          ...prev,
          pendingConversations: new Map(prev.pendingConversations),
          pendingQueueVersions: new Map(prev.pendingQueueVersions),
          activeJobIdsBySession: new Map(prev.activeJobIdsBySession),
        };
        const conversation: ConversationView = {
          conversationId: messageId,
          displayMode: "live",
          sessionId: activeSession.session_id,
          threadId: accepted.thread_id,
          userMessage: {
            message_id: messageId,
            session_id: activeSession.session_id,
            thread_id: accepted.thread_id,
            role: "user",
            content,
            metadata: {
              source: "optimistic",
              job_id: jobId,
              pending_submission_id: pendingSubmissionId,
            },
            attachments,
            created_at: submittedAt,
            updated_at: submittedAt,
          },
          assistantMessages: [],
          events: [],
          status: accepted.status === "queued" ? "queued" : accepted.status === "completed" ? "done"
            : ["failed", "cancelled", "timed_out"].includes(accepted.status) ? "error" : "running",
          jobId,
          turnId: accepted.turn_id ?? undefined,
          pending: !["completed", "failed", "cancelled", "timed_out"].includes(accepted.status),
          activeJobOverlay: !["queued", "completed", "failed", "cancelled", "timed_out"].includes(accepted.status),
          pendingSubmissionId,
          source: "pending",
          ...dispatchToConversationProjection(accepted.dispatch),
        };
        const pendingList = next.pendingConversations.get(activeSessionCacheKey) ?? [];
        next.pendingConversations.set(activeSessionCacheKey, mergeSubmissionConversation(pendingList, conversation));
        writeDispatchActiveJob(
          next.activeJobIdsBySession,
          accepted.dispatch,
          activeSessionCacheKey,
          next.pendingQueueVersions,
        );
        // 当前 Gateway workspace + Session owner 已切走时，旧会话的接受结果不得改写新状态栏；
        // 乐观回合本身仍按 activeSessionCacheKey 落在发起会话自己的队列里。
        if (
          isCurrentSessionOwner(
            prev,
            activeSession.session_id,
            activeSessionGatewayWorkspaceId,
          )
        ) {
          next.status =
            accepted.status === "queued" ? "已排队，等待当前任务结束" : "已发送，等待生成";
        }
        // 普通发送只是向当前历史追加一个回合，不改变 context view。
        // 保留已有时间线，由 pending 镜像、SSE 和目标 Turn 详情增量补齐。
        if (isCurrentSessionOwner(prev, activeSession.session_id, activeSessionGatewayWorkspaceId)) {
          next.contentView = prev.contentView === "agent" ? "default" : prev.contentView;
        }
        return next;
      });
    },
    [
      activeGatewayWorkspaceId,
      apiPort,
      currentSession,
      currentThreadId,
      currentSessionCacheKey,
      currentSessionGatewayWorkspaceId,
      createSession,
      defaultGatewayWorkspaceId,
      setState,
    ],
  );

  const compactSession = useCallback(async (): Promise<SessionCompactResult> => {
    const session = currentSession;
    if (!session) {
      throw new Error("当前没有可压缩上下文的会话");
    }
    const threadId = currentThreadId ?? session.thread_id;

    setState((prev) => ({
      ...prev,
      compactLoading: true,
      status: "正在压缩上下文",
    }));

    try {
      const result = await apiCompactSessionContext(
        apiPort,
        session.session_id,
        currentSessionGatewayWorkspaceId,
        threadId,
      );
      setState((prev) => {
        const next = {
          ...prev,
          eventQueuesBySession: new Map(prev.eventQueuesBySession),
        };
        next.compactLoading = false;
        next.lastCompactResult = result;
        // compactLoading/lastCompactResult 是全局单值，无论会话是否切走都要复位；
        // 但状态栏文案属于当前会话事实，会话已切走时不得写进新会话。
        if (isCurrentSessionOwner(prev, session.session_id, currentSessionGatewayWorkspaceId)) {
          next.status = result.status === "scheduled"
            ? "已安排上下文压缩，将在下一条消息发送前执行"
            : result.status === "compacted"
              ? `已压缩上下文: ${result.summarized_message_count} 条`
              : `上下文未压缩: ${result.message}`;
        }
        appendFrontendEvent(
          next.eventQueuesBySession,
          result.session_id,
          "context_compacted",
          result.status === "scheduled"
            ? "上下文压缩已安排"
            : result.status === "compacted"
              ? "上下文已压缩"
              : "上下文未压缩",
          {
            session_id: result.session_id,
            thread_id: threadId,
            status: result.status,
            before_message_count: result.before_message_count,
            effective_message_count_before: result.effective_message_count_before,
            effective_message_count_after: result.effective_message_count_after,
            summarized_message_count: result.summarized_message_count,
            retained_message_count: result.retained_message_count,
          },
          result.message,
          currentSessionCacheKey ?? result.session_id,
        );
        return next;
      });

      if (contentView === "agent") {
        await refreshAgentStateSnapshot(session.session_id);
      }
      return result;
    } catch (error) {
      const message = errorMessage(error);
      setState((prev) => {
        // compactLoading 必须复位；失败文案同样只在仍停留在发起会话时才写。
        if (!isCurrentSessionOwner(prev, session.session_id, currentSessionGatewayWorkspaceId)) {
          return { ...prev, compactLoading: false };
        }
        return {
          ...prev,
          compactLoading: false,
          status: `上下文压缩失败: ${message}`,
        };
      });
      throw error;
    }
  }, [
    apiPort,
    contentView,
    currentSession,
    currentThreadId,
    currentSessionCacheKey,
    currentSessionGatewayWorkspaceId,
    refreshAgentStateSnapshot,
    setState,
  ]);

  const replayTurn = useCallback(async (
    targetMessageId: string,
    action: MessageReplayRequest["action"],
    displayContent: string,
    content?: string,
    attachments: AttachmentRef[] = [],
  ) => {
    const session = currentSession;
    if (!session) {
      throw new Error("当前没有可操作的会话");
    }
    const workspaceId = currentSessionGatewayWorkspaceId;
    const sessionCacheKey = currentSessionCacheKey ?? session.session_id;
    setState((prev) => ({
      ...prev,
      status: action === "edit_and_continue"
        ? "正在编辑并从此处继续..."
        : action === "regenerate"
          ? "正在重新生成最后回复..."
          : "正在重试失败轮次...",
    }));

    try {
      // TODO: 回放 API 当前只支持主线程，接通精确 Thread 合同后移除此限制。
      if (currentThreadId && currentThreadId !== session.thread_id) throw new Error("子线程轮次回放尚未接入");
      const accepted = await apiReplayMessageTurn(
        apiPort,
        session.session_id,
        targetMessageId,
        {
          action,
          content: action === "edit_and_continue" ? content : undefined,
          acknowledge_context_only: true,
        },
        workspaceId,
      );
      const submittedAt = new Date().toISOString();
      const replacementContent = action === "edit_and_continue"
        ? (content ?? "").trim()
        : displayContent;
      setState((prev) => {
        const next = {
          ...prev,
          pendingConversations: new Map(prev.pendingConversations),
          pendingQueueVersions: new Map(prev.pendingQueueVersions),
          activeJobIdsBySession: new Map(prev.activeJobIdsBySession),
        };
        const conversation: ConversationView = {
          conversationId: accepted.message_id,
          displayMode: "live",
          sessionId: session.session_id,
          threadId: accepted.thread_id,
          userMessage: {
            message_id: accepted.message_id,
            session_id: session.session_id,
            thread_id: accepted.thread_id,
            role: "user",
            content: replacementContent,
            attachments,
            metadata: {
              source: "optimistic_replay",
              job_id: accepted.job_id,
              replay_action: action,
              replaced_message_id: targetMessageId,
            },
            created_at: submittedAt,
            updated_at: submittedAt,
          },
          assistantMessages: [],
          events: [],
          status: "running",
          jobId: accepted.job_id,
          turnId: accepted.turn_id ?? undefined,
          pending: true,
          // replay 已经被后端接受并创建 Job；历史投影刷新期间也必须留在 transcript。
          activeJobOverlay: true,
          source: "pending",
        };
        const pendingList = next.pendingConversations.get(sessionCacheKey) ?? [];
        next.pendingConversations.set(sessionCacheKey, [
          ...pendingList,
          conversation,
        ]);
        writeDispatchActiveJob(
          next.activeJobIdsBySession,
          accepted.dispatch,
          sessionCacheKey,
          next.pendingQueueVersions,
        );
        next.sessionHistoryReloadNonce = prev.sessionHistoryReloadNonce + 1;
        // 会话已切走时，旧会话的回放结果不得改写新会话状态栏；历史重载与乐观
        // 回合本身仍按会话作用域落回发起会话。
        if (isCurrentSessionOwner(prev, session.session_id, workspaceId)) {
          next.status = `${accepted.notice} 正在生成新回复。`;
        }
        return next;
      });
    } catch (error) {
      const message = errorMessage(error);
      // 轮次回放失败只是当前会话的一次动作失败，不属于「工作区初始化失败」；
      // 只写 status，绝不能写 error —— 那是初始化失败出口的专属状态。
      // 这里也不做待处理队列重取：与 sendMessage 不同，replay
      // 没有任何乐观的本地 pending/运行态可失配——乐观回放态只在 apiReplayMessageTurn
      // 成功后才写入，失败发生在请求阶段时前端从未改动队列或 activeJob。后端 replay
      // 是「移除目标消息之后上下文 + 新建替换消息 + 创建新 Job」的多步非原子流程，
      // 中途失败可能已留下半更新（例如替换消息 message_service.create 已提交、
      // checkpoint 回退或相关 Job 因对账被标失败，详见 app/services/business/
      // session_turn_replay_service.py），因此这里不做「一次性多余探测」，避免空快照
      // 结果误覆盖后端真值；而是触发一次历史 bootstrap（sessionHistoryReloadNonce）
      // 让权威历史接管收敛。若将来 replay 引入乐观态，必须改为后端权威校准而非此处
      // 的一次探测。
      setState((prev) => ({
        ...prev,
        sessionHistoryReloadNonce: prev.sessionHistoryReloadNonce + 1,
        // 历史 bootstrap 属于会话作用域，必须触发；失败文案属于当前会话事实，
        // 会话已切走时不得写进新会话状态栏。
        ...(isCurrentSessionOwner(prev, session.session_id, workspaceId)
          ? { status: `轮次操作失败: ${message}` }
          : {}),
      }));
      throw error;
    }
  }, [
    apiPort,
    currentSession,
    currentThreadId,
    currentSessionCacheKey,
    currentSessionGatewayWorkspaceId,
    setState,
  ]);

  return {
    compactSession,
    ...pendingRequestActions,
    replayTurn,
    sendMessage,
  };
}
