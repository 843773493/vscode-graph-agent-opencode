import type {
  SessionTurnBootstrap,
  TurnDetail,
  TurnDetailBatch,
  TurnJobSummary,
  TurnPage,
  TurnSummary,
} from "../../types/backend";

export type TurnRecord = TurnSummary | TurnDetail;
export type TurnTimelinePhase = "idle" | "bootstrapping" | "ready" | "error";
export type TurnProjectionState = NonNullable<
  SessionTurnBootstrap["projection_state"]
>;
export type TurnProjectionEpochDecision = "apply" | "discard_older" | "refresh_bootstrap";

export interface SessionTurnTimeline {
  scopeKey: string;
  generation: number;
  phase: TurnTimelinePhase;
  orderedTurnIds: string[];
  turnsById: Record<string, TurnRecord>;
  activeJobs: TurnJobSummary[];
  olderCursor: string | null;
  hasMore: boolean;
  eventCursor: string | null;
  projectionEpoch: number | null;
  projectionState: TurnProjectionState;
  loadingOlder: boolean;
  loadingDetailIds: string[];
  invalidatedTurnIds: string[];
  mergedTurnIds: string[];
  error: string | null;
}

const TURN_TIMELINE_CACHE_LIMIT = 8;

export function createSessionTurnTimeline(
  scopeKey: string,
  generation = 0,
): SessionTurnTimeline {
  return {
    scopeKey,
    generation,
    phase: "idle",
    orderedTurnIds: [],
    turnsById: {},
    activeJobs: [],
    olderCursor: null,
    hasMore: false,
    eventCursor: null,
    projectionEpoch: null,
    projectionState: "ready",
    loadingOlder: false,
    loadingDetailIds: [],
    invalidatedTurnIds: [],
    mergedTurnIds: [],
    error: null,
  };
}

export function isTurnDetail(turn: TurnRecord): turn is TurnDetail {
  return turn.items_view === "full" || "items" in turn || "final_response" in turn;
}

function sortedTurnIds(turnsById: Record<string, TurnRecord>): string[] {
  return Object.values(turnsById)
    .sort((left, right) =>
      left.ordinal - right.ordinal
      || left.created_at.localeCompare(right.created_at)
      || left.turn_id.localeCompare(right.turn_id),
    )
    .map((turn) => turn.turn_id);
}

function jsonValuesEqual(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((value, index) => jsonValuesEqual(value, right[index]));
  }
  if (
    left === null
    || right === null
    || typeof left !== "object"
    || typeof right !== "object"
  ) {
    return false;
  }
  const leftRecord = left as Record<string, unknown>;
  const rightRecord = right as Record<string, unknown>;
  const leftKeys = Object.keys(leftRecord);
  const rightKeys = Object.keys(rightRecord);
  return leftKeys.length === rightKeys.length
    && leftKeys.every((key) =>
      Object.prototype.hasOwnProperty.call(rightRecord, key)
      && jsonValuesEqual(leftRecord[key], rightRecord[key])
    );
}

function upsertTurnRecord(
  turnsById: Record<string, TurnRecord>,
  incoming: TurnRecord,
): Record<string, TurnRecord> {
  const current = turnsById[incoming.turn_id];
  if (current && current.revision > incoming.revision) {
    return turnsById;
  }
  if (
    current
    && current.revision === incoming.revision
    && isTurnDetail(current)
    && !isTurnDetail(incoming)
  ) {
    return turnsById;
  }
  if (
    current
    && current.revision === incoming.revision
    && isTurnDetail(current) === isTurnDetail(incoming)
  ) {
    if (!jsonValuesEqual(current, incoming)) {
      throw new Error(
        `Turn 同 revision 内容不一致: turn_id=${incoming.turn_id} revision=${incoming.revision}`,
      );
    }
    return turnsById;
  }
  if (current === incoming) {
    return turnsById;
  }
  return { ...turnsById, [incoming.turn_id]: incoming };
}

export function upsertTurn(
  timeline: SessionTurnTimeline,
  incoming: TurnRecord,
): SessionTurnTimeline {
  if (timeline.mergedTurnIds.includes(incoming.turn_id)) {
    return timeline;
  }
  const turnsById = upsertTurnRecord(timeline.turnsById, incoming);
  if (turnsById === timeline.turnsById) {
    return timeline;
  }
  const acceptedRecord = turnsById[incoming.turn_id];
  const knownMergedIds = new Set(timeline.mergedTurnIds);
  const newlyMergedIds = (acceptedRecord.merged_job_ids ?? []).filter(
    (turnId) => turnId
      && turnId !== incoming.turn_id
      && !knownMergedIds.has(turnId),
  );
  const merged = new Set([...timeline.mergedTurnIds, ...newlyMergedIds]);
  const visibleTurnsById = { ...turnsById };
  for (const turnId of merged) {
    delete visibleTurnsById[turnId];
  }
  return {
    ...timeline,
    turnsById: visibleTurnsById,
    orderedTurnIds: sortedTurnIds(visibleTurnsById),
    mergedTurnIds: [...merged],
    loadingDetailIds: timeline.loadingDetailIds.filter(
      (turnId) => !merged.has(turnId),
    ),
    invalidatedTurnIds: timeline.invalidatedTurnIds.filter(
      (turnId) => turnId !== incoming.turn_id && !merged.has(turnId),
    ),
  };
}

const TURN_INVALIDATING_EVENT_TYPES = new Set([
  "job_created",
  "job_merged",
  "job_started",
  "message_created",
  "text_end",
  "tool_call_end",
  "job_completed",
  "job_cancelled",
  "job_failed",
]);

export function turnIdsInvalidatedByEvents(
  events: ReadonlyArray<{ type: string; job_id: string }>,
): string[] {
  return [...new Set(events.flatMap((event) =>
    TURN_INVALIDATING_EVENT_TYPES.has(event.type) && event.job_id
      ? [event.job_id]
      : [],
  ))];
}

function replaceProjection(
  timeline: SessionTurnTimeline,
  projectionEpoch: number,
): SessionTurnTimeline {
  if (
    timeline.projectionEpoch === null
    || timeline.projectionEpoch === projectionEpoch
  ) {
    return timeline;
  }
  return {
    ...createSessionTurnTimeline(timeline.scopeKey, timeline.generation),
    phase: timeline.phase,
    projectionEpoch,
  };
}

export function beginTurnBootstrap(
  timeline: SessionTurnTimeline,
  generation: number,
): SessionTurnTimeline {
  return {
    ...timeline,
    generation,
    phase: "bootstrapping",
    loadingOlder: false,
    loadingDetailIds: [],
    error: null,
  };
}

export function applyTurnBootstrap(
  timeline: SessionTurnTimeline,
  generation: number,
  bootstrap: SessionTurnBootstrap,
): SessionTurnTimeline {
  if (timeline.generation !== generation) {
    return timeline;
  }
  let next = replaceProjection(timeline, bootstrap.projection_epoch);
  const projectionState = bootstrap.projection_state ?? "ready";
  next = {
    ...next,
    phase: projectionState === "ready" ? "ready" : "bootstrapping",
    activeJobs: bootstrap.active_jobs ?? [],
    olderCursor: bootstrap.older_cursor ?? null,
    hasMore: Boolean(bootstrap.older_cursor),
    eventCursor: bootstrap.event_cursor ?? null,
    projectionEpoch: bootstrap.projection_epoch,
    projectionState,
    error: null,
  };
  return bootstrap.latest_turn ? upsertTurn(next, bootstrap.latest_turn) : next;
}

function requireMatchingEpoch(
  timeline: SessionTurnTimeline,
  projectionEpoch: number,
): void {
  if (
    timeline.projectionEpoch !== null
    && timeline.projectionEpoch !== projectionEpoch
  ) {
    throw new Error(
      `Turn 投影 epoch 不一致: 当前 ${timeline.projectionEpoch}，响应 ${projectionEpoch}`,
    );
  }
}

export function applyTurnPage(
  timeline: SessionTurnTimeline,
  page: TurnPage,
): SessionTurnTimeline {
  requireMatchingEpoch(timeline, page.projection_epoch);
  let next = timeline;
  for (const turn of page.items) {
    next = upsertTurn(next, turn);
  }
  return {
    ...next,
    phase: "ready",
    olderCursor: page.next_cursor ?? null,
    hasMore: page.has_more ?? false,
    projectionEpoch: page.projection_epoch,
    projectionState: "ready",
    loadingOlder: false,
    error: null,
  };
}

export function decideTurnProjectionEpoch(
  currentEpoch: number | null,
  responseEpoch: number,
): TurnProjectionEpochDecision {
  if (currentEpoch === null || responseEpoch === currentEpoch) {
    return "apply";
  }
  return responseEpoch < currentEpoch ? "discard_older" : "refresh_bootstrap";
}

export function applyTurnDetails(
  timeline: SessionTurnTimeline,
  batch: TurnDetailBatch,
): SessionTurnTimeline {
  requireMatchingEpoch(timeline, batch.projection_epoch);
  let next = timeline;
  const receivedIds = new Set<string>();
  for (const turn of batch.items) {
    receivedIds.add(turn.turn_id);
    next = upsertTurn(next, turn);
  }
  return {
    ...next,
    projectionEpoch: batch.projection_epoch,
    loadingDetailIds: next.loadingDetailIds.filter(
      (turnId) => !receivedIds.has(turnId),
    ),
    error: null,
  };
}

export function markTurnsLoading(
  timeline: SessionTurnTimeline,
  turnIds: string[],
): SessionTurnTimeline {
  const loading = new Set(timeline.loadingDetailIds);
  for (const turnId of turnIds) {
    loading.add(turnId);
  }
  return { ...timeline, loadingDetailIds: [...loading] };
}

export function invalidateTurn(
  timeline: SessionTurnTimeline,
  turnId: string,
): SessionTurnTimeline {
  if (!turnId || timeline.invalidatedTurnIds.includes(turnId)) {
    return timeline;
  }
  return {
    ...timeline,
    invalidatedTurnIds: [...timeline.invalidatedTurnIds, turnId],
  };
}

export function failTurnTimeline(
  timeline: SessionTurnTimeline,
  generation: number,
  error: string,
): SessionTurnTimeline {
  if (timeline.generation !== generation) {
    return timeline;
  }
  return {
    ...timeline,
    phase: "error",
    loadingOlder: false,
    loadingDetailIds: [],
    error,
  };
}

export function writeTurnTimelineCache(
  timelines: Map<string, SessionTurnTimeline>,
  scopeKey: string,
  timeline: SessionTurnTimeline,
): Map<string, SessionTurnTimeline> {
  const next = new Map(timelines);
  next.delete(scopeKey);
  next.set(scopeKey, timeline);
  while (next.size > TURN_TIMELINE_CACHE_LIMIT) {
    const oldestKey = next.keys().next().value;
    if (typeof oldestKey !== "string") {
      break;
    }
    next.delete(oldestKey);
  }
  return next;
}
