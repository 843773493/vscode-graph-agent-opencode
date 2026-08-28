import type { TurnResponsePart } from "../types/backend";

export type MessageStreamEventType =
  | "stream.opened"
  | "model.started"
  | "model.completed"
  | "model.retrying"
  | "model.failed"
  | "block.started"
  | "block.delta"
  | "block.completed"
  | "tool_call"
  | "tool_call.delta"
  | "tool_call.completed"
  | "tool.started"
  | "tool.completed"
  | "activity.started"
  | "activity.updated"
  | "activity.completed"
  | "activity.failed"
  | "interrupt.requested"
  | "interrupt.rejected"
  | "stream.completed"
  | "stream.interrupted"
  | "stream.failed"
  | "stream.snapshot";

export interface MessageStreamEvent {
  event_id: string;
  session_id: string;
  turn_id: string;
  turn_stream_id: string;
  event_seq: number;
  emitted_at?: string;
  type: MessageStreamEventType;
  model_call_id?: string;
  block_id?: string;
  tool_execution_id?: string;
  job_id?: string;
  payload: Record<string, unknown>;
}

export interface MessageStreamLifecycle {
  started_seq?: number;
  last_event_seq?: number;
  completed_seq?: number;
  started_at?: string;
  updated_at?: string;
  completed_at?: string;
}

export interface MessageStreamBlock extends MessageStreamLifecycle {
  block_id: string;
  model_call_id: string | null;
  block_index: number;
  carrier_type: string;
  status: "running" | "completed" | "failed" | "interrupted";
  text: string;
  items: Record<string, unknown>[];
  redacted: boolean;
  projection: string;
  completion_reason?: string;
  partial?: boolean;
}

export interface MessageStreamToolExecution extends MessageStreamLifecycle {
  tool_execution_id: string;
  tool_call_id: string;
  tool_name: string;
  status: "running" | "completed" | "failed";
  outcome?: "success" | "provider_error" | "execution_lost" | "outcome_unknown";
  completion_reason?: string;
  result?: string;
  error?: string;
}

export interface MessageStreamActivity extends MessageStreamLifecycle {
  activity_id: string;
  kind: string;
  parent_activity_id?: string;
  scope_ref: string;
  status: "running" | "waiting" | "stopping" | "completed" | "failed" | "unknown";
  outcome?: string;
  summary?: string;
  cancellable: boolean;
  resumable: boolean;
  side_effect_policy: string;
  resource_refs: string[];
  detail?: Record<string, unknown>;
  detail_ref?: string;
  detail_available: boolean;
  detail_error?: string;
  completion_reason?: string;
}

export interface MessageStreamActiveState {
  kind: string;
  phase: string;
  entity_id: string;
  carrier_type?: string;
  block_id?: string;
  tool_call_id?: string;
  tool_execution_id?: string;
  activity_id?: string;
  activity_kind?: string;
  status: string;
  last_kind?: string;
  last_phase?: string;
  reason?: string;
  detail_ref?: string;
}

export interface MessageStreamState {
  sessionId: string;
  turnId: string;
  turnStreamId: string;
  lastEventSeq: number;
  streamStatus: "open" | "interrupting" | "completed" | "interrupted" | "failed";
  agentLoopStatus: string;
  currentModelCallId: string | null;
  currentAttempt: number;
  blocks: MessageStreamBlock[];
  toolExecutions: MessageStreamToolExecution[];
  toolCalls: Record<string, Record<string, unknown>>;
  interruptState: {
    requestId: string | null;
    status: string;
    reason?: string;
    factConfirmed?: boolean;
  } | null;
  failure: {
    code: string;
    message: string;
    afterInterruptRequested: boolean;
    resumable: boolean;
  } | null;
  activeState: MessageStreamActiveState | null;
  activities: MessageStreamActivity[];
  modelCalls: Record<string, Record<string, unknown>>;
  resourceRefs: Record<string, Record<string, unknown>>;
  recovery: Record<string, unknown> | null;
  pendingEvents: MessageStreamEvent[];
  resumable: boolean;
  connectionStatus: "connecting" | "connected" | "disconnected" | "gap" | "terminal";
  protocolError: string | null;
}

export function createMessageStreamState(
  sessionId: string,
  turnId: string,
  turnStreamId: string = "",
): MessageStreamState {
  return {
    sessionId,
    turnId,
    turnStreamId,
    lastEventSeq: 0,
    streamStatus: "open",
    agentLoopStatus: "running",
    currentModelCallId: null,
    currentAttempt: 0,
    blocks: [],
    toolExecutions: [],
    toolCalls: {},
    interruptState: null,
    failure: null,
    activeState: null,
    activities: [],
    modelCalls: {},
    resourceRefs: {},
    recovery: null,
    pendingEvents: [],
    resumable: true,
    connectionStatus: "connecting",
    protocolError: null,
  };
}

export function applyMessageStreamEvent(
  current: MessageStreamState | null,
  event: MessageStreamEvent,
): MessageStreamState {
  const state = current
    ? cloneMessageStreamState(current)
    : createMessageStreamState(event.session_id, event.turn_id);
  if (state.sessionId !== event.session_id || state.turnId !== event.turn_id) {
    return {
      ...state,
      connectionStatus: "gap",
      protocolError: "消息流关联键与当前 Turn 不一致",
    };
  }
  if (state.turnStreamId && state.turnStreamId !== event.turn_stream_id) {
    return {
      ...state,
      connectionStatus: "gap",
      protocolError: "消息流 turn_stream_id 在同一 Turn 内发生变化",
    };
  }
  if (event.type === "stream.snapshot") {
    const snapshot = isRecord(event.payload.snapshot)
      ? event.payload.snapshot
      : event.payload;
    const snapshotSeq = numberValue(snapshot.snapshot_seq) ?? event.event_seq;
    if (snapshotSeq < state.lastEventSeq) return state;
    return drainPendingEvents(applySnapshot(state, event));
  }
  if (event.event_seq <= state.lastEventSeq) return state;
  if (event.event_seq !== state.lastEventSeq + 1) {
    const pendingEvents = state.pendingEvents.filter(
      (pending) => pending.event_id !== event.event_id,
    );
    pendingEvents.push(event);
    pendingEvents.sort((left, right) => left.event_seq - right.event_seq);
    return {
      ...state,
      connectionStatus: "gap",
      pendingEvents,
      protocolError: `消息流 event_seq 不连续: expected=${state.lastEventSeq + 1} actual=${event.event_seq}`,
    };
  }

  state.lastEventSeq = event.event_seq;
  state.turnStreamId = event.turn_stream_id;
  state.connectionStatus = isTerminalEvent(event.type) ? "terminal" : "connected";
  state.protocolError = null;
  const payload = event.payload;
  switch (event.type) {
    case "stream.opened":
      state.streamStatus = "open";
      break;
    case "model.started":
      state.currentModelCallId = stringValue(payload.model_call_id) ?? event.model_call_id ?? null;
      state.currentAttempt = numberValue(payload.attempt) ?? state.currentAttempt;
      state.agentLoopStatus = "model_running";
      upsertModelCall(state, payload, "running", event);
      state.activeState = {
        kind: "model_output",
        phase: "reasoning",
        entity_id: state.currentModelCallId ?? "",
        status: "running",
      };
      break;
    case "model.completed":
      state.agentLoopStatus = "validating";
      upsertModelCall(state, payload, "completed", event);
      state.activeState = activeStateAfter(state.activeState, "model_output", "validating", "completed");
      break;
    case "model.retrying":
      state.agentLoopStatus = "retrying";
      for (const block of state.blocks) {
        if (block.model_call_id === state.currentModelCallId) {
          block.projection = "intermediate";
        }
      }
      break;
    case "model.failed":
      state.agentLoopStatus = "failed";
      state.failure = failureFromPayload(payload);
      upsertModelCall(state, payload, "failed", event);
      break;
    case "block.started":
      {
        const block = upsertBlock(
        state,
        event.model_call_id
          ? { ...payload, model_call_id: event.model_call_id }
          : payload,
        "running",
        event,
        );
        if (block) {
          state.activeState = {
            kind: "model_output",
            phase: modelOutputPhase(block.carrier_type),
            entity_id: block.block_id,
            block_id: block.block_id,
            carrier_type: block.carrier_type,
            status: "running",
          };
        }
      }
      break;
    case "block.delta":
      applyBlockDelta(state, payload, event);
      break;
    case "block.completed": {
      const block = findBlock(state, stringValue(payload.block_id) ?? event.block_id ?? null);
      if (block) {
        block.status = blockStatusValue(payload.status);
        block.completion_reason = stringValue(payload.completion_reason) ?? "upstream_completed";
        block.partial = booleanValue(payload.partial) ?? false;
        applyLifecycle(block, event, true);
        state.activeState = activeStateAfter(
          state.activeState,
          "model_output",
          "completed",
          block.status,
          block.block_id,
        );
      }
      break;
    }
    case "tool_call":
    case "tool_call.delta": {
      const toolCallId = stringValue(payload.tool_call_id);
      if (toolCallId) {
        state.toolCalls[toolCallId] = mergeToolCall(
          state.toolCalls[toolCallId],
          payload,
        );
        applyLifecycle(state.toolCalls[toolCallId], event);
        state.activeState = {
          kind: "tool_call",
          phase: "arguments",
          entity_id: toolCallId,
          tool_call_id: toolCallId,
          status: "running",
        };
      }
      break;
    }
    case "tool_call.completed": {
      const toolCallId = stringValue(payload.tool_call_id);
      if (toolCallId) {
        state.toolCalls[toolCallId] = mergeToolCall(
          state.toolCalls[toolCallId],
          payload,
        );
        applyLifecycle(state.toolCalls[toolCallId], event, true);
        state.activeState = activeStateAfter(
          state.activeState,
          "tool_call",
          "completed",
          stringValue(payload.status) ?? "completed",
          toolCallId,
        );
      }
      break;
    }
    case "tool.started":
      upsertTool(state, payload, "running", event);
      state.agentLoopStatus = "tool_running";
      state.activeState = {
        kind: "tool_execution",
        phase: "running",
        entity_id: stringValue(payload.tool_execution_id) ?? "",
        tool_call_id: stringValue(payload.tool_call_id) ?? undefined,
        tool_execution_id: stringValue(payload.tool_execution_id) ?? undefined,
        status: "running",
      };
      break;
    case "tool.completed":
      upsertTool(state, payload, toolExecutionStatus(payload.status), event);
      state.activeState = activeStateAfter(
        state.activeState,
        "tool_execution",
        "completed",
        toolExecutionStatus(payload.status),
        stringValue(payload.tool_execution_id) ?? undefined,
      );
      break;
    case "activity.started":
    case "activity.updated":
    case "activity.completed":
    case "activity.failed":
      upsertActivity(state, payload, event);
      break;
    case "interrupt.requested":
      state.streamStatus = "interrupting";
      state.activeState = {
        kind: "interrupt",
        phase: "requested",
        entity_id: stringValue(payload.interrupt_request_id) ?? "",
        status: "requested",
        reason: stringValue(payload.reason) ?? undefined,
      };
      state.interruptState = {
        requestId: stringValue(payload.interrupt_request_id),
        status: "requested",
        reason: stringValue(payload.reason) ?? undefined,
        factConfirmed: false,
      };
      break;
    case "interrupt.rejected":
      state.interruptState = {
        requestId: stringValue(payload.interrupt_request_id),
        status: "rejected",
        reason: stringValue(payload.reason) ?? undefined,
        factConfirmed: false,
      };
      break;
    case "stream.completed":
      state.streamStatus = "completed";
      state.agentLoopStatus = "completed";
      state.resumable = false;
      state.activeState = activeStateAfter(state.activeState, "stream", "completed", "completed");
      break;
    case "stream.interrupted":
      state.streamStatus = "interrupted";
      state.agentLoopStatus = "interrupted";
      state.resumable = false;
      finishRunningModelCalls(state, event, "user_interrupt");
      finishRunningBlocks(state, "interrupted", event);
      finishRunningToolCalls(state, event, "user_interrupt");
      markRunningToolsUnknown(state, event);
      finishRunningActivities(state, event, "user_interrupt");
      state.interruptState = {
        requestId: stringValue(payload.interrupt_request_id),
        status: "confirmed",
        factConfirmed: true,
      };
      state.activeState = activeStateAfter(state.activeState, "stream", "interrupted", "interrupted");
      break;
    case "stream.failed":
      state.streamStatus = "failed";
      state.agentLoopStatus = "failed";
      state.failure = failureFromPayload(payload);
      state.resumable = booleanValue(payload.resumable) ?? false;
      finishRunningModelCalls(state, event, "execution_lost");
      finishRunningBlocks(state, "failed", event);
      finishRunningToolCalls(state, event, "execution_lost");
      markRunningToolsUnknown(state, event);
      finishRunningActivities(state, event, "execution_lost");
      state.activeState = activeStateAfter(state.activeState, "stream", "failed", "failed");
      break;
  }
  return drainPendingEvents(state);
}

function applySnapshot(
  current: MessageStreamState,
  event: MessageStreamEvent,
): MessageStreamState {
  const payload = event.payload;
  const snapshot = isRecord(payload.snapshot) ? payload.snapshot : payload;
  const next = createMessageStreamState(current.sessionId, current.turnId, event.turn_stream_id);
  next.lastEventSeq = numberValue(snapshot.snapshot_seq) ?? event.event_seq;
  next.streamStatus = streamStatusValue(snapshot.stream_status);
  next.agentLoopStatus = stringValue(snapshot.agent_loop_status) ?? "running";
  next.currentModelCallId = stringValue(snapshot.current_model_call_id);
  next.currentAttempt = numberValue(snapshot.current_attempt) ?? 0;
  next.blocks = sortBlocks(arrayValue(snapshot.blocks).flatMap(blockFromSnapshot));
  next.toolCalls = toolCallsFromSnapshot(snapshot.tool_calls);
  next.toolExecutions = sortToolExecutions(
    arrayValue(snapshot.tool_executions).flatMap(toolFromSnapshot),
  );
  next.activeState = activeStateFromSnapshot(snapshot.active_state);
  next.activities = sortActivities(arrayValue(snapshot.activities).flatMap(activityFromSnapshot));
  next.modelCalls = modelCallsFromSnapshot(snapshot.model_calls);
  next.resourceRefs = resourceRefsFromSnapshot(snapshot.resource_refs);
  next.recovery = isRecord(snapshot.recovery) ? { ...snapshot.recovery } : null;
  const interruptState = isRecord(snapshot.interrupt_state)
    ? snapshot.interrupt_state
    : null;
  if (interruptState) {
    next.interruptState = {
      requestId: stringValue(interruptState.request_id),
      status: stringValue(interruptState.status) ?? "unknown",
      reason: stringValue(interruptState.reason) ?? undefined,
      factConfirmed: booleanValue(interruptState.fact_confirmed) ?? undefined,
    };
  }
  next.failure = failureFromUnknown(snapshot.failure);
  next.resumable = booleanValue(snapshot.resumable) ?? false;
  next.pendingEvents = current.pendingEvents.filter(
    (pending) => pending.event_seq > next.lastEventSeq,
  );
  const firstPendingSeq = next.pendingEvents[0]?.event_seq;
  const hasPendingGap = firstPendingSeq !== undefined
    && firstPendingSeq > next.lastEventSeq + 1;
  next.connectionStatus = isTerminalStatus(next.streamStatus)
    ? "terminal"
    : hasPendingGap ? "gap" : "connected";
  next.protocolError = hasPendingGap
    ? `消息流 event_seq 不连续: expected=${next.lastEventSeq + 1} actual=${firstPendingSeq}`
    : null;
  return next;
}

export function messageStreamToResponseParts(
  state: MessageStreamState,
): TurnResponsePart[] {
  const parts: TurnResponsePart[] = [];
  const executionCallIds = new Set(
    state.toolExecutions.map((execution) => execution.tool_call_id),
  );
  const entities: MessageStreamResponseEntity[] = [
    ...sortBlocks(state.blocks).map((block) => ({
      kind: "block" as const,
      value: block,
      fallback: block.block_index,
      id: block.block_id,
    })),
    ...sortToolExecutions(state.toolExecutions).map((execution) => ({
      kind: "tool_execution" as const,
      value: execution,
      fallback: 0,
      id: execution.tool_execution_id,
    })),
    ...sortedToolCallEntries(state.toolCalls)
      .filter(([toolCallId, toolCall]) => {
        if (executionCallIds.has(toolCallId)) return false;
        const callStatus = stringValue(toolCall.status);
        return callStatus === "incomplete"
          || callStatus === "cancelled"
          || toolCall.arguments_complete === false;
      })
      .map(([id, value]) => ({
        kind: "tool_call" as const,
        value,
        fallback: 0,
        id,
      })),
  ];
  entities.sort(compareResponseEntities);

  for (const entity of entities) {
    if (entity.kind === "block") {
      const block = entity.value;
      if (block.projection === "intermediate" || block.projection === "superseded") continue;
      const kind = block.redacted || block.carrier_type === "redacted_thinking"
        ? "reasoning_encrypted"
        : block.carrier_type === "text"
          ? "text"
          : block.carrier_type === "reasoning_items"
            ? "reasoning_summary"
            : "reasoning";
      const structuredText = block.items
        .map((item) => stringValue(item.text) ?? stringValue(item.content) ?? stringValue(item.summary) ?? "")
        .filter(Boolean)
        .join("\n");
      const text = block.redacted ? "" : block.text || structuredText;
      if (text || block.redacted || block.items.length > 0) {
        parts.push({
          part_id: block.block_id,
          kind,
          projection: "streaming",
          status: block.status === "completed" || block.status === "running"
            ? block.status
            : "failed",
          source: {
            message_sequence: 0,
            content_block_index: block.block_index,
          },
          text,
          carrier_type: block.carrier_type,
          final: block.status === "completed" && !block.partial,
        });
      }
      continue;
    }
    if (entity.kind === "tool_execution") {
      const execution = entity.value;
      const toolCall = state.toolCalls[execution.tool_call_id];
      const argumentsValue = toolCall?.arguments;
      const status = responsePartToolStatus(execution);
      const outcomeUnknown = execution.outcome === "outcome_unknown";
      parts.push({
        part_id: execution.tool_execution_id,
        kind: "tool_call",
        projection: "streaming",
        status,
        source: { message_sequence: 0 },
        text: "",
        tool_call_id: execution.tool_call_id,
        tool_name: execution.tool_name,
        arguments: typeof argumentsValue === "string"
          ? argumentsValue
          : argumentsValue ? JSON.stringify(argumentsValue) : "",
        outcome_unknown: outcomeUnknown,
        final: status === "completed",
      });
      if (execution.result || execution.error) {
        parts.push({
          part_id: `${execution.tool_execution_id}:result`,
          kind: "tool_result",
          projection: "streaming",
          status,
          source: { message_sequence: 0 },
          text: execution.result ?? execution.error ?? "",
          result: execution.result ?? execution.error ?? "",
          tool_call_id: execution.tool_call_id,
          tool_name: execution.tool_name,
          outcome_unknown: outcomeUnknown,
          final: status === "completed",
        });
      }
      continue;
    }
    const toolCallId = entity.id;
    const toolCall = entity.value;
    const callStatus = stringValue(toolCall.status);
    const argumentsValue = toolCall.arguments;
    parts.push({
      part_id: toolCallId,
      kind: "tool_call",
      projection: "streaming",
      status: callStatus === "cancelled" ? "cancelled" : "failed",
      source: { message_sequence: 0 },
      text: "",
      tool_call_id: toolCallId,
      tool_name: stringValue(toolCall.tool_name) ?? "tool",
      arguments: typeof argumentsValue === "string"
        ? argumentsValue
        : isRecord(argumentsValue) ? JSON.stringify(argumentsValue) : "",
      outcome_unknown: false,
      final: true,
    });
  }
  return parts;
}

type MessageStreamResponseEntity =
  | {
    kind: "block";
    value: MessageStreamBlock;
    fallback: number;
    id: string;
  }
  | {
    kind: "tool_execution";
    value: MessageStreamToolExecution;
    fallback: number;
    id: string;
  }
  | {
    kind: "tool_call";
    value: Record<string, unknown>;
    fallback: number;
    id: string;
  };

function lifecycleFromValue(value: unknown): MessageStreamLifecycle {
  if (!isRecord(value)) return {};
  const lifecycle: MessageStreamLifecycle = {};
  const startedSeq = lifecycleSequence(value, "started_seq");
  const lastEventSeq = lifecycleSequence(value, "last_event_seq");
  const completedSeq = lifecycleSequence(value, "completed_seq");
  if (startedSeq !== null) lifecycle.started_seq = startedSeq;
  if (lastEventSeq !== null) lifecycle.last_event_seq = lastEventSeq;
  if (completedSeq !== null) lifecycle.completed_seq = completedSeq;
  const startedAt = stringValue(value.started_at);
  const updatedAt = stringValue(value.updated_at);
  const completedAt = stringValue(value.completed_at);
  if (startedAt) lifecycle.started_at = startedAt;
  if (updatedAt) lifecycle.updated_at = updatedAt;
  if (completedAt) lifecycle.completed_at = completedAt;
  return lifecycle;
}

function lifecycleSequence(value: unknown, field: string): number | null {
  if (!isRecord(value)) return null;
  const sequence = numberValue(value[field]);
  return sequence !== null && sequence >= 0 ? sequence : null;
}

function applyLifecycle(
  entity: MessageStreamLifecycle | Record<string, unknown>,
  event: MessageStreamEvent,
  completed = false,
): void {
  const mutableEntity = entity as Record<string, unknown>;
  const eventSeq = event.event_seq;
  if (!Number.isFinite(eventSeq) || eventSeq < 0) return;
  const timestamp = event.emitted_at;
  if (lifecycleSequence(mutableEntity, "started_seq") === null) {
    mutableEntity.started_seq = eventSeq;
    if (timestamp) mutableEntity.started_at = timestamp;
  }
  const lastEventSeq = lifecycleSequence(mutableEntity, "last_event_seq");
  if (lastEventSeq === null || eventSeq >= lastEventSeq) {
    mutableEntity.last_event_seq = eventSeq;
    if (timestamp) mutableEntity.updated_at = timestamp;
  }
  if (completed && lifecycleSequence(mutableEntity, "completed_seq") === null) {
    mutableEntity.completed_seq = eventSeq;
    if (timestamp) mutableEntity.completed_at = timestamp;
  }
}

function sortBlocks(blocks: readonly MessageStreamBlock[]): MessageStreamBlock[] {
  return sortByLifecycle(blocks, (block) => block.block_index, (block) => block.block_id);
}

function sortToolExecutions(
  executions: readonly MessageStreamToolExecution[],
): MessageStreamToolExecution[] {
  return sortByLifecycle(executions, () => 0, (execution) => execution.tool_execution_id);
}

function sortActivities(activities: readonly MessageStreamActivity[]): MessageStreamActivity[] {
  return sortByLifecycle(activities, () => 0, (activity) => activity.activity_id);
}

function sortedToolCallEntries(
  toolCalls: Record<string, Record<string, unknown>>,
): Array<[string, Record<string, unknown>]> {
  return [...Object.entries(toolCalls)].sort(([leftId, left], [rightId, right]) =>
    compareLifecycleEntities(
      { value: left, fallback: 0, id: leftId },
      { value: right, fallback: 0, id: rightId },
    ));
}

function sortByLifecycle<T>(
  values: readonly T[],
  fallback: (value: T) => number,
  id: (value: T) => string,
): T[] {
  return [...values].sort((left, right) => compareLifecycleEntities(
    { value: left, fallback: fallback(left), id: id(left) },
    { value: right, fallback: fallback(right), id: id(right) },
  ));
}

function compareResponseEntities(
  left: MessageStreamResponseEntity,
  right: MessageStreamResponseEntity,
): number {
  return compareLifecycleEntities(left, right);
}

function compareLifecycleEntities(
  left: { value: unknown; fallback: number; id: string },
  right: { value: unknown; fallback: number; id: string },
): number {
  const leftSeq = lifecycleSequence(left.value, "started_seq");
  const rightSeq = lifecycleSequence(right.value, "started_seq");
  if (leftSeq !== null && rightSeq !== null && leftSeq !== rightSeq) {
    return leftSeq - rightSeq;
  }
  if (leftSeq !== null && rightSeq === null) return -1;
  if (leftSeq === null && rightSeq !== null) return 1;
  return left.fallback - right.fallback || left.id.localeCompare(right.id);
}

function cloneMessageStreamState(state: MessageStreamState): MessageStreamState {
  return {
    ...state,
    blocks: state.blocks.map((block) => ({ ...block, items: block.items.map((item) => ({ ...item })) })),
    toolExecutions: state.toolExecutions.map((tool) => ({ ...tool })),
    toolCalls: Object.fromEntries(
      Object.entries(state.toolCalls).map(([id, value]) => [id, { ...value }]),
    ),
    interruptState: state.interruptState ? { ...state.interruptState } : null,
    failure: state.failure ? { ...state.failure } : null,
    activeState: state.activeState ? { ...state.activeState } : null,
    activities: state.activities.map((activity) => ({
      ...activity,
      resource_refs: [...activity.resource_refs],
      detail: activity.detail ? { ...activity.detail } : undefined,
    })),
    modelCalls: Object.fromEntries(
      Object.entries(state.modelCalls).map(([id, value]) => [id, { ...value }]),
    ),
    resourceRefs: Object.fromEntries(
      Object.entries(state.resourceRefs).map(([id, value]) => [id, { ...value }]),
    ),
    recovery: state.recovery ? { ...state.recovery } : null,
    pendingEvents: state.pendingEvents.map((pending) => ({
      ...pending,
      payload: { ...pending.payload },
    })),
  };
}

function findBlock(state: MessageStreamState, blockId: string | null): MessageStreamBlock | undefined {
  return blockId ? state.blocks.find((block) => block.block_id === blockId) : undefined;
}

function upsertBlock(
  state: MessageStreamState,
  payload: Record<string, unknown>,
  status: MessageStreamBlock["status"],
  event: MessageStreamEvent,
): MessageStreamBlock | null {
  const blockId = stringValue(payload.block_id);
  if (!blockId) return null;
  const existing = findBlock(state, blockId);
  if (existing) {
    existing.status = status;
    applyLifecycle(existing, event);
    return existing;
  }
  const block: MessageStreamBlock = {
    block_id: blockId,
    model_call_id: stringValue(payload.model_call_id),
    block_index: numberValue(payload.block_index) ?? state.blocks.length,
    carrier_type: stringValue(payload.carrier_type) ?? "text",
    status,
    text: "",
    items: [],
    redacted: booleanValue(payload.redacted) ?? false,
    projection: stringValue(payload.projection) ?? "streaming",
    completion_reason: stringValue(payload.completion_reason) ?? undefined,
    partial: booleanValue(payload.partial) ?? false,
    ...lifecycleFromValue(payload),
  };
  state.blocks.push(block);
  applyLifecycle(block, event);
  return block;
}

function applyBlockDelta(
  state: MessageStreamState,
  payload: Record<string, unknown>,
  event: MessageStreamEvent,
): void {
  const block = upsertBlock(state, payload, "running", event);
  if (!block) return;
  state.activeState = {
    kind: "model_output",
    phase: modelOutputPhase(block.carrier_type),
    entity_id: block.block_id,
    block_id: block.block_id,
    carrier_type: block.carrier_type,
    status: "running",
  };
  const operation = stringValue(payload.operation) ?? "append";
  if (operation === "append" && typeof payload.text === "string") {
    block.text += payload.text;
  }
  if (operation === "redacted" || payload.redacted === true) block.redacted = true;
  if (operation === "item_upsert" || operation === "item_patch") {
    const item = isRecord(payload.item) ? payload.item : null;
    if (!item) return;
    const itemId = stringValue(item.id);
    const index = itemId
      ? block.items.findIndex((existing) => existing.id === itemId)
      : -1;
    if (index === -1) block.items.push({ ...item });
    else block.items[index] = operation === "item_patch"
      ? { ...block.items[index], ...item }
      : { ...item };
  }
}

function upsertTool(
  state: MessageStreamState,
  payload: Record<string, unknown>,
  status: MessageStreamToolExecution["status"],
  event: MessageStreamEvent,
): void {
  const executionId = stringValue(payload.tool_execution_id);
  if (!executionId) return;
  const existing = state.toolExecutions.find((tool) => tool.tool_execution_id === executionId);
  if (existing) {
    Object.assign(existing, toolFromPayload(payload, status));
    applyLifecycle(existing, event, status !== "running");
    return;
  }
  const execution = toolFromPayload(payload, status);
  applyLifecycle(execution, event, status !== "running");
  state.toolExecutions.push(execution);
}

function markRunningToolsUnknown(
  state: MessageStreamState,
  event: MessageStreamEvent,
): void {
  for (const execution of state.toolExecutions) {
    if (execution.status === "running") {
      execution.status = "completed";
      execution.outcome = "outcome_unknown";
      execution.completion_reason = "execution_lost";
      applyLifecycle(execution, event, true);
    }
  }
}

function finishRunningBlocks(
  state: MessageStreamState,
  status: "failed" | "interrupted",
  event: MessageStreamEvent,
): void {
  for (const block of state.blocks) {
    if (block.status === "running") {
      block.status = status;
      block.completion_reason = status === "interrupted" ? "user_interrupt" : "execution_lost";
      block.partial = true;
      applyLifecycle(block, event, true);
    }
  }
}

function finishRunningModelCalls(
  state: MessageStreamState,
  event: MessageStreamEvent,
  outcome: "user_interrupt" | "execution_lost",
): void {
  for (const modelCall of Object.values(state.modelCalls)) {
    if (stringValue(modelCall.status) !== "running") continue;
    modelCall.status = "failed";
    modelCall.outcome = outcome;
    modelCall.retryable = false;
    modelCall.completion_reason = outcome;
    applyLifecycle(modelCall, event, true);
  }
}

function finishRunningToolCalls(
  state: MessageStreamState,
  event: MessageStreamEvent,
  reason: "user_interrupt" | "execution_lost",
): void {
  for (const toolCall of Object.values(state.toolCalls)) {
    const status = stringValue(toolCall.status);
    if (status !== "accumulating" && status !== "streaming" && status !== "running") continue;
    toolCall.status = reason === "user_interrupt" && toolCall.arguments_complete === true
      ? "cancelled"
      : "incomplete";
    toolCall.completion_reason = reason;
    applyLifecycle(toolCall, event, true);
  }
}

function finishRunningActivities(
  state: MessageStreamState,
  event: MessageStreamEvent,
  reason: "user_interrupt" | "execution_lost",
): void {
  for (const activity of state.activities) {
    if (activity.status !== "running" && activity.status !== "waiting" && activity.status !== "stopping") continue;
    const externallySideEffecting = !["none", "read_only"].includes(activity.side_effect_policy);
    const uncertainInterrupt = reason === "user_interrupt" && externallySideEffecting;
    activity.status = reason === "execution_lost" || uncertainInterrupt ? "failed" : "completed";
    activity.outcome = reason === "execution_lost"
      ? "execution_lost"
      : uncertainInterrupt ? "outcome_unknown" : reason;
    activity.completion_reason = reason;
    if (activity.status === "failed") activity.resumable = false;
    applyLifecycle(activity, event, true);
  }
}

function toolFromPayload(
  payload: Record<string, unknown>,
  status: MessageStreamToolExecution["status"],
): MessageStreamToolExecution {
  return {
    tool_execution_id: stringValue(payload.tool_execution_id) ?? "unknown-tool-execution",
    tool_call_id: stringValue(payload.tool_call_id) ?? "unknown-tool-call",
    tool_name: stringValue(payload.tool_name) ?? "tool",
    status,
    outcome: toolExecutionOutcome(payload.outcome),
    completion_reason: stringValue(payload.completion_reason) ?? undefined,
    result: stringValue(payload.result) ?? undefined,
    error: stringValue(payload.error) ?? undefined,
    ...lifecycleFromValue(payload),
  };
}

function blockFromSnapshot(value: unknown): MessageStreamBlock[] {
  if (!isRecord(value) || !stringValue(value.block_id)) return [];
  return [{
    block_id: stringValue(value.block_id)!,
    model_call_id: stringValue(value.model_call_id),
    block_index: numberValue(value.block_index) ?? 0,
    carrier_type: stringValue(value.carrier_type) ?? "text",
    status: blockStatusValue(value.status),
    text: stringValue(value.text) ?? "",
    items: arrayValue(value.items).filter(isRecord),
    redacted: booleanValue(value.redacted) ?? false,
    projection: stringValue(value.projection) ?? "streaming",
    completion_reason: stringValue(value.completion_reason) ?? undefined,
    partial: booleanValue(value.partial) ?? false,
    ...lifecycleFromValue(value),
  }];
}

function toolFromSnapshot(value: unknown): MessageStreamToolExecution[] {
  if (!isRecord(value) || !stringValue(value.tool_execution_id)) return [];
  return [toolFromPayload(value, toolExecutionStatus(value.status))];
}

function toolCallsFromSnapshot(value: unknown): Record<string, Record<string, unknown>> {
  const calls: Record<string, Record<string, unknown>> = {};
  for (const item of arrayValue(value)) {
    if (!isRecord(item)) continue;
    const toolCallId = stringValue(item.tool_call_id);
    if (toolCallId) calls[toolCallId] = { ...item };
  }
  return calls;
}

function mergeToolCall(
  previous: Record<string, unknown> | undefined,
  incoming: Record<string, unknown>,
): Record<string, unknown> {
  if (!previous) return { ...incoming };
  const merged = { ...previous, ...incoming };
  if (stringValue(previous.tool_name) && !stringValue(incoming.tool_name)) {
    merged.tool_name = previous.tool_name;
  }
  if (hasArguments(previous.arguments) && !hasArguments(incoming.arguments)) {
    merged.arguments = previous.arguments;
  }
  return merged;
}

function hasArguments(value: unknown): boolean {
  if (typeof value === "string") return value.length > 0;
  return isRecord(value) ? Object.keys(value).length > 0 : value != null;
}

function failureFromPayload(payload: Record<string, unknown>): MessageStreamState["failure"] {
  return failureFromUnknown(payload);
}

function failureFromUnknown(value: unknown): MessageStreamState["failure"] {
  if (!isRecord(value)) return null;
  const message = stringValue(value.message);
  if (!message) return null;
  return {
    code: stringValue(value.code) ?? "message_stream_failure",
    message,
    afterInterruptRequested: booleanValue(value.after_interrupt_requested) ?? false,
    resumable: booleanValue(value.resumable) ?? false,
  };
}

function isTerminalEvent(type: MessageStreamEventType): boolean {
  return type === "stream.completed" || type === "stream.interrupted" || type === "stream.failed";
}

function isTerminalStatus(status: MessageStreamState["streamStatus"]): boolean {
  return status === "completed" || status === "interrupted" || status === "failed";
}

function streamStatusValue(value: unknown): MessageStreamState["streamStatus"] {
  return value === "interrupting" || value === "completed" || value === "interrupted" || value === "failed"
    ? value
    : "open";
}

function blockStatusValue(value: unknown): MessageStreamBlock["status"] {
  return value === "completed" || value === "failed" || value === "interrupted"
    ? value
    : "running";
}

function toolExecutionStatus(value: unknown): MessageStreamToolExecution["status"] {
  if (value === "failed") return "failed";
  if (value === "completed" || value === "succeeded" || value === "outcome_unknown") {
    return "completed";
  }
  return "running";
}

function toolExecutionOutcome(value: unknown): MessageStreamToolExecution["outcome"] {
  return value === "success"
    || value === "provider_error"
    || value === "execution_lost"
    || value === "outcome_unknown"
    ? value
    : undefined;
}

function responsePartToolStatus(
  execution: MessageStreamToolExecution,
): "running" | "completed" | "failed" {
  if (execution.status === "failed") return "failed";
  if (execution.outcome === "outcome_unknown" || execution.outcome === "provider_error") {
    return "failed";
  }
  return execution.status === "completed" ? "completed" : "running";
}

function modelOutputPhase(carrierType: string): string {
  return [
    "reasoning",
    "reasoning_content",
    "reasoning_items",
    "thinking",
    "redacted_thinking",
  ].includes(carrierType) ? "reasoning" : "text";
}

function activeStateAfter(
  previous: MessageStreamActiveState | null,
  kind: string,
  phase: string,
  status: string,
  entityId?: string,
): MessageStreamActiveState {
  return {
    kind,
    phase,
    entity_id: entityId ?? previous?.entity_id ?? "",
    carrier_type: previous?.carrier_type,
    block_id: previous?.block_id,
    tool_call_id: previous?.tool_call_id,
    tool_execution_id: previous?.tool_execution_id,
    activity_id: previous?.activity_id,
    activity_kind: previous?.activity_kind,
    status,
    last_kind: previous?.kind,
    last_phase: previous?.phase,
    reason: previous?.reason,
    detail_ref: previous?.detail_ref,
  };
}

function activeStateFromSnapshot(value: unknown): MessageStreamActiveState | null {
  if (!isRecord(value)) return null;
  const kind = stringValue(value.kind);
  const phase = stringValue(value.phase);
  if (!kind || !phase) return null;
  return {
    kind,
    phase,
    entity_id: stringValue(value.entity_id) ?? "",
    carrier_type: stringValue(value.carrier_type) ?? undefined,
    block_id: stringValue(value.block_id) ?? undefined,
    tool_call_id: stringValue(value.tool_call_id) ?? undefined,
    tool_execution_id: stringValue(value.tool_execution_id) ?? undefined,
    activity_id: stringValue(value.activity_id) ?? undefined,
    activity_kind: stringValue(value.activity_kind) ?? undefined,
    status: stringValue(value.status) ?? "unknown",
    last_kind: stringValue(value.last_kind) ?? undefined,
    last_phase: stringValue(value.last_phase) ?? undefined,
    reason: stringValue(value.reason) ?? undefined,
    detail_ref: stringValue(value.detail_ref) ?? undefined,
  };
}

function activityFromSnapshot(value: unknown): MessageStreamActivity[] {
  if (!isRecord(value)) return [];
  const activityId = stringValue(value.activity_id);
  const kind = stringValue(value.kind);
  if (!activityId || !kind) return [];
  return [{
    activity_id: activityId,
    kind,
    parent_activity_id: stringValue(value.parent_activity_id) ?? undefined,
    scope_ref: stringValue(value.scope_ref) ?? "turn",
    status: activityStatusValue(value.status),
    outcome: stringValue(value.outcome) ?? undefined,
    summary: stringValue(value.summary) ?? undefined,
    cancellable: booleanValue(value.cancellable) ?? false,
    resumable: booleanValue(value.resumable) ?? false,
    side_effect_policy: stringValue(value.side_effect_policy) ?? "unknown",
    resource_refs: arrayValue(value.resource_refs).filter(
      (item): item is string => typeof item === "string",
    ),
    detail: isRecord(value.detail) ? { ...value.detail } : undefined,
    detail_ref: stringValue(value.detail_ref) ?? undefined,
    detail_available: booleanValue(value.detail_available) ?? false,
    detail_error: stringValue(value.detail_error) ?? undefined,
    completion_reason: stringValue(value.completion_reason) ?? undefined,
    ...lifecycleFromValue(value),
  }];
}

function upsertActivity(
  state: MessageStreamState,
  payload: Record<string, unknown>,
  event: MessageStreamEvent,
): void {
  const activity = activityFromSnapshot(payload)[0];
  if (!activity) return;
  const existing = state.activities.find((item) => item.activity_id === activity.activity_id);
  if (existing) {
    Object.assign(existing, activity);
    applyLifecycle(existing, event, event.type === "activity.completed" || event.type === "activity.failed");
  } else {
    applyLifecycle(activity, event, event.type === "activity.completed" || event.type === "activity.failed");
    state.activities.push(activity);
  }
  state.activeState = {
    kind: "activity",
    phase: activity.status,
    entity_id: activity.activity_id,
    activity_id: activity.activity_id,
    activity_kind: activity.kind,
    status: activity.status,
    detail_ref: activity.detail_ref,
  };
}

function activityStatusValue(value: unknown): MessageStreamActivity["status"] {
  return value === "running"
    || value === "waiting"
    || value === "stopping"
    || value === "completed"
    || value === "failed"
    || value === "unknown"
    ? value
    : "unknown";
}

function modelCallsFromSnapshot(value: unknown): Record<string, Record<string, unknown>> {
  const calls: Record<string, Record<string, unknown>> = {};
  for (const item of arrayValue(value)) {
    if (!isRecord(item)) continue;
    const id = stringValue(item.model_call_id);
    if (id) calls[id] = { ...item };
  }
  return calls;
}

function upsertModelCall(
  state: MessageStreamState,
  payload: Record<string, unknown>,
  status: string,
  event: MessageStreamEvent,
): void {
  const id = stringValue(payload.model_call_id);
  if (!id) return;
  state.modelCalls[id] = {
    ...state.modelCalls[id],
    ...payload,
    status,
  };
  applyLifecycle(
    state.modelCalls[id],
    event,
    status === "completed" || status === "failed",
  );
}

function resourceRefsFromSnapshot(value: unknown): Record<string, Record<string, unknown>> {
  const refs: Record<string, Record<string, unknown>> = {};
  for (const item of arrayValue(value)) {
    if (!isRecord(item)) continue;
    const id = stringValue(item.resource_id);
    if (id) refs[id] = { ...item };
  }
  return refs;
}

function drainPendingEvents(state: MessageStreamState): MessageStreamState {
  if (state.pendingEvents.length === 0) return state;
  let next: MessageStreamState = { ...state, pendingEvents: [] };
  for (const pending of [...state.pendingEvents].sort((left, right) => left.event_seq - right.event_seq)) {
    if (pending.event_seq === next.lastEventSeq + 1) {
      next = applyMessageStreamEvent(next, pending);
    } else if (pending.event_seq > next.lastEventSeq) {
      next.pendingEvents.push(pending);
    }
  }
  if (next.pendingEvents.length === 0 && next.connectionStatus === "gap") {
    next.connectionStatus = isTerminalStatus(next.streamStatus) ? "terminal" : "connected";
    next.protocolError = null;
  }
  return next;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function booleanValue(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}
