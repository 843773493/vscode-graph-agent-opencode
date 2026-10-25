import type {
  JobDispatchSnapshot,
  JobStatus,
  PendingRequestList,
  TraceEvent,
} from "../../types/backend";
import type { ConversationView } from "../../types/frontend";
import { dedupeTraceEvents, isJobTerminalTraceType } from "../traceEvents";
import { conversationMatchKeys, TERMINAL_TURN_STATUSES } from "./conversationMerge";
import { writePendingList } from "./pendingMap";
import type { TurnRecord } from "../session/turnTimeline";
import {
  compactPendingConversationEvents,
  conversationMatchesTraceEvent,
  hasJobTerminalTraceEvent,
} from "./traceProjection";

/**
 * 后端 JobDispatchSnapshot 直投到 ConversationView 的队列事实字段。
 *
 * 成功派发后前端必须以后端返回的完整对象替换本地状态，这里只做一次整体投影，
 * 不再逐字段手挑；后端未提供的可选值一律保持 undefined，不回退到请求参数或
 * 任何本地默认值，避免向前端伪造后端并不存在的队列事实。
 */
export function dispatchToConversationProjection(
  dispatch: JobDispatchSnapshot,
): Pick<
  ConversationView,
  | "deliveryPolicy"
  | "enqueueSequence"
  | "pendingPosition"
  | "queueSnapshotVersion"
  | "queuedJobCount"
  | "pendingJobCount"
  | "blockedByJobId"
> {
  return {
    deliveryPolicy: dispatch.delivery_policy ?? undefined,
    enqueueSequence: dispatch.enqueue_sequence ?? undefined,
    pendingPosition: dispatch.queued_jobs_ahead,
    queueSnapshotVersion: dispatch.queue_snapshot_version,
    queuedJobCount: dispatch.queued_job_count,
    pendingJobCount: dispatch.pending_job_count,
    blockedByJobId: dispatch.blocked_by_job_id,
  };
}

/** 把后端 dispatch 的活动 Job 写回会话级运行态镜像；dispatch 未给出活动 Job 时保持现状。 */
export function writeDispatchActiveJob(
  activeJobMap: Map<string, string>,
  dispatch: JobDispatchSnapshot,
  mapKey: string,
): void {
  if (dispatch.active_job_id) {
    activeJobMap.set(mapKey, dispatch.active_job_id);
  }
}

export function pendingSnapshotToConversations(
  snapshot: PendingRequestList,
): ConversationView[] {
  if (!snapshot.thread_id) {
    throw new Error("主视图待处理快照缺少精确 thread 范围");
  }
  const pendingRequests = snapshot.requests ?? [];
  if (pendingRequests.some((request) =>
    request.session_id !== snapshot.session_id || request.thread_id !== snapshot.thread_id
  )) {
    throw new Error("主视图待处理快照包含范围之外的消息");
  }
  const orderedRequests = [...pendingRequests].sort(
    (left, right) => left.enqueue_sequence - right.enqueue_sequence,
  );
  const conversations: ConversationView[] = [];
  for (const request of orderedRequests) {
    // 内部准入的 turn_id 明确为空；其展示只来自 standalone history envelope。
    if (request.turn_id === null) {
      continue;
    }
    const messageUpdatedAt = request.message_metadata?.updated_at;
    if (typeof messageUpdatedAt !== "string" || !messageUpdatedAt) {
      throw new Error(`待处理消息缺少 canonical updated_at: message_id=${request.message_id}`);
    }
    conversations.push({
      conversationId: request.message_id,
      displayMode: "live" as const,
      sessionId: request.session_id,
      threadId: request.thread_id,
      userMessage: {
        message_id: request.message_id,
        session_id: request.session_id,
        thread_id: request.thread_id,
        role: "user",
        content: request.content,
        attachments: request.attachments ?? [],
        metadata: {
          ...request.message_metadata,
          source: "pending",
          job_id: request.job_id,
          delivery_policy: request.delivery_policy,
        },
        created_at: request.message_created_at,
        updated_at: messageUpdatedAt,
      },
      assistantMessages: [],
      events: [],
      status: "queued",
      jobId: request.job_id,
      turnId: request.turn_id ?? undefined,
      pending: true,
      deliveryPolicy: request.delivery_policy,
      enqueueSequence: request.enqueue_sequence,
      waitingReason: request.waiting_reason,
      queueSnapshotVersion: snapshot.snapshot_version,
      pendingPosition: request.position,
      source: "pending",
    });
  }
  return conversations;
}

export function writePendingSnapshot(
  pendingMap: Map<string, ConversationView[]>,
  activeJobMap: Map<string, string>,
  snapshot: PendingRequestList,
  mapKey: string = snapshot.session_id,
) {
  const incomingConversations = pendingSnapshotToConversations(snapshot);
  const existingPending = pendingMap.get(mapKey) ?? [];
  if (existingPending.some((conversation) =>
    conversation.sessionId !== snapshot.session_id
    || (conversation.threadId && conversation.threadId !== snapshot.thread_id)
  )) {
    throw new Error("待处理快照与既有 live 对话的 Session/thread 范围不一致");
  }
  let existingSnapshotVersion = 0;
  for (const conversation of existingPending) {
    existingSnapshotVersion = Math.max(
      existingSnapshotVersion,
      conversation.queueSnapshotVersion ?? 0,
    );
  }
  if ((snapshot.snapshot_version ?? 0) < existingSnapshotVersion) {
    return;
  }
  const existingPendingByJobId = new Map<string, ConversationView>();
  for (const conversation of existingPending) {
    if (conversation.jobId && !existingPendingByJobId.has(conversation.jobId)) {
      existingPendingByJobId.set(conversation.jobId, conversation);
    }
  }
  const snapshotConversations: ConversationView[] = [];
  for (const conversation of incomingConversations) {
    const live = conversation.jobId
      ? existingPendingByJobId.get(conversation.jobId)
      : undefined;
    if (!live) {
      snapshotConversations.push(conversation);
      continue;
    }
    if (live.turnId && conversation.turnId && live.turnId !== conversation.turnId) {
      throw new Error("待处理快照与既有 live 对话的 Job/Turn 归属不一致");
    }
    const terminal = hasJobTerminalTraceEvent(live.events)
      || TERMINAL_TURN_STATUSES.has(live.turnStatus ?? "");
    // 快照完整替换队列事实；同一 owner 已收到的 live 内容和事件独立续存。
    // root/revision/view 坐标只由当前 timeline 投影。
    snapshotConversations.push({
      ...live,
      ...conversation,
      assistantMessages: live.assistantMessages,
      events: live.events,
      status: terminal ? live.status
        : conversation.jobId === snapshot.active_job_id ? "running" : conversation.status,
      pending: !terminal,
      activeJobOverlay: !terminal && conversation.jobId === snapshot.active_job_id,
    });
  }
  const existingActiveConversation = snapshot.active_job_id
    ? existingPendingByJobId.get(snapshot.active_job_id)
    : undefined;
  if (
    snapshot.active_job_id
    && existingActiveConversation
    && !snapshotConversations.some(
      (conversation) => conversation.jobId === snapshot.active_job_id,
    )
  ) {
    snapshotConversations.push({
      ...existingActiveConversation,
      pending: true,
      pendingPosition: undefined,
      deliveryPolicy: undefined,
      activeJobOverlay: true,
    });
  }
  // replay 的新 Job 可能尚未进入 bootstrap/pending-requests 快照，但后端已经
  // 移除了旧上下文。保留乐观 replay，避免上下文切换期间出现空聊天区。
  const retainedConversationKeys = new Set<string>();
  const retainIfUnmatched = (conversation: ConversationView): void => {
    const keys = conversationMatchKeys(conversation);
    if (keys.some((key) => retainedConversationKeys.has(key))) return;
    snapshotConversations.push(conversation);
    for (const key of keys) retainedConversationKeys.add(key);
  };
  for (const conversation of snapshotConversations) {
    for (const key of conversationMatchKeys(conversation)) {
      retainedConversationKeys.add(key);
    }
  }
  for (const conversation of existingPending) {
    if (
      conversation.pending
      && conversation.source === "pending"
      && Boolean(conversation.jobId)
      && conversation.userMessage?.metadata?.source === "optimistic_replay"
    ) {
      retainIfUnmatched(conversation);
    }
  }
  // 终态回合仍由实时消息流或 Trace 提供当前视图；下一次 bootstrap 会用
  // canonical 历史替换它，不能被空 pending 快照提前删掉。
  for (const conversation of existingPending) {
    if (
      conversation.source === "pending"
      && Boolean(conversation.jobId)
      && (
        hasJobTerminalTraceEvent(conversation.events)
        || TERMINAL_TURN_STATUSES.has(conversation.turnStatus ?? "")
      )
    ) {
      retainIfUnmatched(conversation);
    }
  }
  writePendingList(
    pendingMap,
    snapshot.session_id,
    snapshotConversations,
    mapKey,
  );
  if (snapshot.active_job_id) {
    activeJobMap.set(mapKey, snapshot.active_job_id);
  } else {
    activeJobMap.delete(mapKey);
  }
}

export function syncActiveJobConversation(
  map: Map<string, ConversationView[]>,
  sessionId: string,
  activeJobId: string | null,
  mapKey: string,
  activeTurn: TurnRecord | null,
  threadId: string,
): void {
  if (activeJobId && activeTurn
    && (activeTurn.job_id !== activeJobId || activeTurn.session_id !== sessionId)) {
    throw new Error("活动 Job 与权威 Turn owner 不一致");
  }
  const existing = map.get(mapKey) ?? [];
  if (existing.some((conversation) => conversation.sessionId !== sessionId)) {
    throw new Error("活动 Job 与既有 live 对话的 Session 范围不一致");
  }
  const retained = existing.filter(
    (conversation) =>
      conversation.threadId !== threadId || !conversation.activeJobOverlay || conversation.jobId === activeJobId,
  ).map((conversation) => conversation.jobId === activeJobId && conversation.threadId === threadId
    ? {
        ...conversation,
        turnId: activeTurn?.turn_id ?? conversation.turnId,
        pending: true,
        pendingPosition: undefined,
        deliveryPolicy: undefined,
        activeJobOverlay: true,
      }
    : conversation);
  if (
    activeJobId
    && activeTurn
    && !retained.some((conversation) => conversation.jobId === activeJobId && conversation.threadId === threadId)
  ) {
    retained.push({
      conversationId: activeTurn.turn_id,
      sessionId,
      threadId,
      turnId: activeTurn.turn_id,
      userMessage: null,
      assistantMessages: [],
      events: [],
      status: "running",
      jobId: activeJobId,
      displayMode: "live",
      pending: true,
      activeJobOverlay: true,
      source: "pending",
    });
  }
  writePendingList(map, sessionId, retained, mapKey);
}

export function removePendingForTraceEvent(
  map: Map<string, ConversationView[]>,
  sessionId: string,
  event: TraceEvent,
  mapKey: string = sessionId,
) {
  if (!isJobTerminalTraceType(event.type)) {
    return;
  }
  const pendingList = map.get(mapKey) ?? [];
  if (pendingList.length === 0) {
    return;
  }

  writePendingList(
    map,
    sessionId,
    pendingList.filter(
      (conversation) => !conversationMatchesTraceEvent(conversation, event),
    ),
    mapKey,
  );
}

export function preservePendingTerminalConversation(
  map: Map<string, ConversationView[]>,
  sessionId: string,
  event: TraceEvent,
  turnStatus: Extract<JobStatus, "completed" | "failed" | "cancelled" | "timed_out">,
  mapKey: string = sessionId,
): void {
  const pendingList = map.get(mapKey) ?? [];
  const pendingIndex = pendingList.findIndex((conversation) =>
    conversationMatchesTraceEvent(conversation, event),
  );
  if (pendingIndex === -1) return;

  const pending = pendingList[pendingIndex];
  const events = compactPendingConversationEvents(
    dedupeTraceEvents([...pending.events, event]),
  );
  const updatedPending: ConversationView = {
    ...pending,
    events,
    status: turnStatus === "completed" ? "done" : "error",
    turnStatus,
    pending: false,
    activeJobOverlay: false,
  };
  const next = [...pendingList];
  next[pendingIndex] = updatedPending;
  writePendingList(map, sessionId, next, mapKey);
}

export function completePendingForJob(
  map: Map<string, ConversationView[]>,
  sessionId: string,
  jobId: string,
  turnStatus: Extract<JobStatus, "completed" | "failed" | "cancelled" | "timed_out">,
  mapKey: string,
  threadId: string,
): void {
  if (!jobId) return;
  const pendingList = map.get(mapKey) ?? [];
  const pendingIndex = pendingList.findIndex(
    (conversation) => conversation.jobId === jobId && conversation.threadId === threadId,
  );
  if (pendingIndex === -1) return;

  const next = [...pendingList];
  next[pendingIndex] = {
    ...next[pendingIndex],
    status: turnStatus === "completed" ? "done" : "error",
    turnStatus,
    pending: false,
    activeJobOverlay: false,
  };
  writePendingList(map, sessionId, next, mapKey);
}
