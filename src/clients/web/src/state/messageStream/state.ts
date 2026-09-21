// 消息流状态构造与共享原语：构造、克隆、缓存淘汰、生命周期、排序与值归一。
import type {
  MessageStreamActivity,
  MessageStreamBlock,
  MessageStreamEvent,
  MessageStreamLifecycle,
  MessageStreamState,
  MessageStreamToolExecution,
} from "./types";

export const MESSAGE_STREAM_PENDING_EVENT_LIMIT = 256;
const TERMINAL_MESSAGE_STREAM_CACHE_LIMIT = 8;
export const MESSAGE_STREAM_BLOCK_TEXT_MAX_CHARS = 256 * 1024;
export const MESSAGE_STREAM_TOOL_TEXT_MAX_CHARS = 64 * 1024;
const MESSAGE_STREAM_TEXT_TRUNCATION_MARKER =
  "\n\n…消息流展示已截断；Turn 完成后可从权威历史详情读取持久化内容…\n\n";

export function boundedMessageStreamText(value: string, maxChars: number): string {
  if (value.length <= maxChars) return value;
  const retainedChars = maxChars - MESSAGE_STREAM_TEXT_TRUNCATION_MARKER.length;
  const headChars = Math.floor(retainedChars / 2);
  const tailChars = retainedChars - headChars;
  return `${value.slice(0, headChars)}${MESSAGE_STREAM_TEXT_TRUNCATION_MARKER}${value.slice(-tailChars)}`;
}

export function writeMessageStreamCache(
  streams: Map<string, MessageStreamState>,
  key: string,
  state: MessageStreamState,
): Map<string, MessageStreamState> {
  const next = new Map(streams);
  for (const [existingKey, existing] of next.entries()) {
    if (
      existingKey !== key
      && existing.sessionId === state.sessionId
      && existing.turnId === state.turnId
    ) {
      next.delete(existingKey);
    }
  }
  next.delete(key);
  next.set(key, state);

  const terminalKeys = [...next.entries()]
    .filter(([, value]) => isTerminalStatus(value.streamStatus))
    .map(([streamKey]) => streamKey);
  while (terminalKeys.length > TERMINAL_MESSAGE_STREAM_CACHE_LIMIT) {
    const oldestKey = terminalKeys.shift();
    if (oldestKey !== undefined && oldestKey !== key) {
      next.delete(oldestKey);
    }
  }
  return next;
}

export function createMessageStreamState(
  sessionId: string,
  turnId: string,
  turnStreamId: string = "",
): MessageStreamState {
  return {
    workspaceId: null,
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

export function cloneMessageStreamState(state: MessageStreamState): MessageStreamState {
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
    pendingEvents: state.pendingEvents.map((pending): MessageStreamEvent =>
      pending.type === "stream.snapshot"
        ? { ...pending, payload: { ...pending.payload } }
        : { ...pending, payload: { ...pending.payload } },
    ),
  };
}

export function findBlock(state: MessageStreamState, blockId: string | null): MessageStreamBlock | undefined {
  return blockId ? state.blocks.find((block) => block.block_id === blockId) : undefined;
}

export function lifecycleFromValue(value: unknown): MessageStreamLifecycle {
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

export function lifecycleSequence(value: unknown, field: string): number | null {
  if (!isRecord(value)) return null;
  const sequence = numberValue(value[field]);
  return sequence !== null && sequence >= 0 ? sequence : null;
}

export function applyLifecycle(
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

export function sortBlocks(blocks: readonly MessageStreamBlock[]): MessageStreamBlock[] {
  return sortByLifecycle(blocks, (block) => block.block_index, (block) => block.block_id);
}

export function sortToolExecutions(
  executions: readonly MessageStreamToolExecution[],
): MessageStreamToolExecution[] {
  return sortByLifecycle(executions, () => 0, (execution) => execution.tool_execution_id);
}

export function sortActivities(activities: readonly MessageStreamActivity[]): MessageStreamActivity[] {
  return sortByLifecycle(activities, () => 0, (activity) => activity.activity_id);
}

export function sortedToolCallEntries(
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

export function compareLifecycleEntities(
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

export function isTerminalStatus(status: MessageStreamState["streamStatus"]): boolean {
  return status === "completed" || status === "interrupted" || status === "failed";
}

export function blockStatusValue(value: unknown): MessageStreamBlock["status"] {
  return value === "completed" || value === "failed" || value === "interrupted"
    ? value
    : "running";
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

export function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function booleanValue(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}
