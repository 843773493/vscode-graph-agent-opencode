import type {
  SessionTurnBootstrap,
  TurnDetail,
  TurnDetailBatch,
  TurnHistoryPage,
  TurnJobSummary,
  TurnPage,
  TurnSummary,
  InternalExecutionHistory,
} from "../../types/backend";
import { compareInternalResponseParts } from "../timeline/responseParts";

export type TurnRecord = TurnSummary | TurnDetail;
export type TurnTimelinePhase = "idle" | "bootstrapping" | "ready" | "error";
export type TurnProjectionState = NonNullable<
  SessionTurnBootstrap["projection_state"]
>;
export type TurnProjectionEpochDecision = "apply" | "discard_older" | "refresh_bootstrap";

export interface SessionTurnTimeline {
  scopeKey: string;
  threadId: string | null;
  historyViewRevision: number | null;
  generation: number;
  phase: TurnTimelinePhase;
  orderedTurnIds: string[];
  turnsById: Record<string, TurnRecord>;
  /** 后端原样返回的摘要投影，用于淘汰已展开详情，禁止前端自行重建摘要。 */
  summaryTurnsById: Record<string, TurnSummary>;
  /** Turn 之外的 internal execution 展示记录；仅由独立 history DTO 提供。 */
  internalExecutions: InternalExecutionHistory[];
  detailAccessOrder: string[];
  activeJobs: TurnJobSummary[];
  beforeCursor: string | null;
  afterCursor: string | null;
  hasBefore: boolean;
  hasAfter: boolean;
  loadingBefore: boolean;
  loadingAfter: boolean;
  eventCursor: string | null;
  projectionEpoch: number | null;
  projectionState: TurnProjectionState;
  loadingDetailIds: string[];
  invalidatedTurnIds: string[];
  mergedTurnIds: string[];
  error: string | null;
}

const TURN_TIMELINE_CACHE_LIMIT = 64;
const TURN_DETAIL_CACHE_MAX_COUNT = 8;
const TURN_DETAIL_CACHE_MAX_PROJECTION_UNITS = 1_000_000;

export function createSessionTurnTimeline(
  scopeKey: string,
  generation = 0,
): SessionTurnTimeline {
  return {
    scopeKey,
    threadId: null,
    historyViewRevision: null,
    generation,
    phase: "idle",
    orderedTurnIds: [],
    turnsById: {},
    summaryTurnsById: {},
    internalExecutions: [],
    detailAccessOrder: [],
    activeJobs: [],
    beforeCursor: null,
    afterCursor: null,
    hasBefore: false,
    hasAfter: false,
    loadingBefore: false,
    loadingAfter: false,
    eventCursor: null,
    projectionEpoch: null,
    projectionState: "ready",
    loadingDetailIds: [],
    invalidatedTurnIds: [],
    mergedTurnIds: [],
    error: null,
  };
}

export function isTurnDetail(turn: TurnRecord): turn is TurnDetail {
  return turn.items_view === "full"
    || "items" in turn
    || (turn.items_view !== "summary" && "final_response" in turn);
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

type TurnDetailItem = NonNullable<TurnDetail["items"]>[number];

function projectionRecord(turn: TurnRecord): Record<string, unknown> {
  return turn as unknown as Record<string, unknown>;
}

function isEmptyProjectionValue(value: unknown): boolean {
  if (value === undefined || value === null) return true;
  if (typeof value === "string") return value.length === 0;
  if (Array.isArray(value)) return value.length === 0;
  if (typeof value === "object") return Object.keys(value).length === 0;
  return false;
}

function projectionRichness(value: unknown): number {
  if (isEmptyProjectionValue(value)) return 0;
  if (typeof value === "string") return value.length;
  if (Array.isArray(value)) {
    return value.reduce(
      (total, item) => total + 1 + projectionRichness(item),
      0,
    );
  }
  if (typeof value === "object" && value !== null) {
    return Object.entries(value).reduce(
      (total, [key, item]) => total + key.length + projectionRichness(item),
      0,
    );
  }
  return 1;
}

/**
 * 同一 revision 可能先后返回摘要投影和详情投影。
 * 空字段不能覆盖已经加载的内容；非空字段取信息量更高的一侧。
 */
function preferProjectionValue<T>(
  current: T | undefined,
  incoming: T | undefined,
): T | undefined {
  if (isEmptyProjectionValue(incoming)) return current;
  if (isEmptyProjectionValue(current)) return incoming;
  return projectionRichness(incoming) >= projectionRichness(current)
    ? incoming
    : current;
}

function mergeTurnItems(
  current: TurnDetailItem[] | undefined,
  incoming: TurnDetailItem[] | undefined,
): TurnDetailItem[] | undefined {
  if (!current?.length) return incoming;
  if (!incoming?.length) return current;

  // 初始投影会用 tool_summary 生成没有 raw/content 的占位事件；详情请求
  // 返回真实 tool_call/tool_result 后，不能把两套事件同时交给 React。
  // 否则同一个 part_id 会在 ThinkingSection 中出现两次，并触发重复 key。
  const isSummaryPlaceholder = (item: TurnDetailItem): boolean =>
    item.event_id.includes(":tool_summary:");
  const isMaterialized = (item: TurnDetailItem): boolean =>
    Object.keys(item.raw ?? {}).length > 0 || Boolean(item.content);
  const itemKey = (item: TurnDetailItem): string =>
    `${item.part_id ?? item.event_id}:${item.type}`;
  const materializedPartIds = new Set(
    incoming
      .filter(isMaterialized)
      .map(itemKey),
  );
  const currentItems = current.filter((item) =>
    !isSummaryPlaceholder(item) || !materializedPartIds.has(itemKey(item)),
  );
  const currentPartIds = new Set(
    currentItems
      .map(itemKey),
  );
  const incomingItems = incoming.filter((item) =>
    !isSummaryPlaceholder(item) || !currentPartIds.has(itemKey(item)),
  );

  const merged = new Map<string, TurnDetailItem>();
  for (const item of currentItems) {
    merged.set(itemKey(item), item);
  }
  for (const item of incomingItems) {
    const previous = merged.get(itemKey(item));
    if (!previous) {
      merged.set(itemKey(item), item);
      continue;
    }
    merged.set(
      itemKey(item),
      projectionRichness(item) >= projectionRichness(previous)
        ? item
        : previous,
    );
  }
  return [...merged.values()];
}

function partIdOf(part: unknown): string | null {
  const value = (part as { part_id?: unknown } | null)?.part_id;
  return typeof value === "string" && value ? value : null;
}

/**
 * 定点工具详情请求只返回被请求的工具部件（携带 tool_call_ids）。若把它的
 * response_parts 直接整体替换，会把同一 Turn 的 reasoning、final 等部件一并
 * 丢掉。这里按稳定 part_id 把定点结果补丁进现有部件：保持现有部件的后端顺序，
 * 命中的部件用定点结果覆盖（拿到 arguments/result），未命中的新部件追加到末尾。
 *
 * 只有当 incoming 的每个 part_id 都已在 current 中时才使用补丁语义（说明这是
 * 对既有部件的定点补载，而不是引入新部件的整轮详情）。否则仍按契约整体替换，
 * 避免把旧 summary 与新 detail 拼成第三套历史。
 */
export function patchResponseParts(
  current: readonly unknown[] | undefined | null,
  incoming: readonly unknown[],
): unknown[] {
  const currentList = Array.isArray(current) ? current : [];
  if (currentList.length === 0) return [...incoming];
  const incomingById = new Map<string, unknown>();
  let hasUnidentifiedPart = false;
  for (const part of incoming) {
    const id = partIdOf(part);
    if (id) incomingById.set(id, part);
    else hasUnidentifiedPart = true;
  }
  const currentIds = new Set<string>();
  for (const part of currentList) {
    const id = partIdOf(part);
    if (id) currentIds.add(id);
  }
  const isPointPatch = !hasUnidentifiedPart
    && incomingById.size > 0
    && [...incomingById.keys()].every((id) => currentIds.has(id));
  if (!isPointPatch) {
    return [...incoming];
  }
  const patched: unknown[] = currentList.map((part) => {
    const id = partIdOf(part);
    if (!id) return part;
    return incomingById.get(id) ?? part;
  });
  return patched;
}

function mergeResponseParts(
  current: unknown,
  incoming: unknown,
): unknown[] | undefined {
  if (Array.isArray(incoming)) {
    // 同 revision 的 include 投影可能只请求 metadata，空部件不撤销已加载正文。
    if (incoming.length === 0 && Array.isArray(current)) return [...current];
    // 历史响应已经携带 canonical item identity 和后端顺序。前端不得再按
    // message 坐标重排、按正文去重，或把旧 summary 与新 detail 拼成第三套历史。
    return Array.isArray(current) ? patchResponseParts(current, incoming) : [...incoming];
  }
  return Array.isArray(current) ? [...current] : undefined;
}

function mergeSameRevisionTurn(
  current: TurnRecord,
  incoming: TurnRecord,
): TurnRecord {
  const currentRecord = projectionRecord(current);
  const incomingRecord = projectionRecord(incoming);
  const merged = { ...currentRecord, ...incomingRecord };

  const projectionFields = [
    "source_message_ids",
    "merged_job_ids",
    "user_messages",
    "response_preview",
    "assistant_text",
    "thinking_blocks",
    "tool_summary",
    "response_parts",
    "final_response",
  ];
  for (const field of projectionFields) {
    if (field === "response_parts") {
      const mergedResponseParts = mergeResponseParts(
        currentRecord[field],
        incomingRecord[field],
      );
      if (mergedResponseParts !== undefined) merged[field] = mergedResponseParts;
      continue;
    }
    const value = preferProjectionValue(
      currentRecord[field],
      incomingRecord[field],
    );
    if (value !== undefined) {
      merged[field] = value;
    }
  }

  const items = mergeTurnItems(
    currentRecord.items as TurnDetailItem[] | undefined,
    incomingRecord.items as TurnDetailItem[] | undefined,
  );
  if (items !== undefined) {
    merged.items = items;
  }

  if (
    currentRecord.items_view === "full"
    || incomingRecord.items_view === "full"
    || items !== undefined
  ) {
    merged.items_view = "full";
  } else {
    merged.items_view = "summary";
  }

  const mergedRecord = merged as unknown as TurnRecord;
  return jsonValuesEqual(current, mergedRecord) ? current : mergedRecord;
}

function upsertTurnRecord(
  turnsById: Record<string, TurnRecord>,
  incoming: TurnRecord,
): boolean {
  const current = turnsById[incoming.turn_id];
  if (current && current.source_view_revision > incoming.source_view_revision) {
    return false;
  }
  if (
    current
    && current.source_view_id === incoming.source_view_id
    && current.source_view_revision === incoming.source_view_revision
  ) {
    if (current.revision > incoming.revision) {
      return false;
    }
    if (current.revision === incoming.revision) {
      const merged = mergeSameRevisionTurn(current, incoming);
      if (merged === current) {
        return false;
      }
      turnsById[incoming.turn_id] = merged;
      return true;
    }
  }
  if (current === incoming) {
    return false;
  }
  turnsById[incoming.turn_id] = incoming;
  return true;
}

export function upsertTurn(
  timeline: SessionTurnTimeline,
  incoming: TurnRecord,
): SessionTurnTimeline {
  return upsertTurns(timeline, [incoming]);
}

/**
 * 一次性合并历史页，避免每个 Turn 都重新排序整个 timeline。
 * 历史页通常包含固定锚点窗口，排序和可见 ID 计算只能在批次末尾执行一次。
 */
export function upsertTurns(
  timeline: SessionTurnTimeline,
  incomingTurns: readonly TurnRecord[],
): SessionTurnTimeline {
  if (incomingTurns.length === 0) {
    return timeline;
  }
  const freshTurns = timeline.historyViewRevision === null
    ? incomingTurns
    : incomingTurns.filter((turn) =>
        turn.source_view_revision >= timeline.historyViewRevision!
      );
  const historyViewRevision = timeline.threadId === null || freshTurns.length === 0
    ? timeline.historyViewRevision
    : Math.max(
        timeline.historyViewRevision ?? 0,
        ...freshTurns.map((turn) => turn.source_view_revision),
      );
  const historyRevisionChanged = historyViewRevision !== timeline.historyViewRevision;
  if (freshTurns.length === 0) {
    return historyRevisionChanged
      ? { ...timeline, historyViewRevision }
      : timeline;
  }
  let turnsById = { ...timeline.turnsById };
  let summaryTurnsById = { ...timeline.summaryTurnsById };
  let detailAccessOrder = [...timeline.detailAccessOrder];
  const mergedTurnIds = new Set(timeline.mergedTurnIds);
  let changed = false;

  for (const incoming of freshTurns) {
    if (
      mergedTurnIds.has(incoming.turn_id)
      || timeline.invalidatedTurnIds.includes(incoming.turn_id)
    ) {
      continue;
    }
    if (!isTurnDetail(incoming)) {
      const previousSummary = summaryTurnsById[incoming.turn_id];
      const summaryRecords: Record<string, TurnRecord> = {
        ...(previousSummary ? { [incoming.turn_id]: previousSummary } : {}),
      };
      if (upsertTurnRecord(summaryRecords, incoming)) {
        summaryTurnsById[incoming.turn_id] = summaryRecords[incoming.turn_id] as TurnSummary;
        changed = true;
      }
    } else {
      const previousOrder = detailAccessOrder;
      detailAccessOrder = detailAccessOrder.filter((turnId) => turnId !== incoming.turn_id);
      detailAccessOrder.push(incoming.turn_id);
      if (
        previousOrder.length !== detailAccessOrder.length
        || previousOrder.some((turnId, index) => turnId !== detailAccessOrder[index])
      ) {
        changed = true;
      }
    }
    if (upsertTurnRecord(turnsById, incoming)) {
      changed = true;
    }
    const acceptedRecord = turnsById[incoming.turn_id];
    const newlyMergedIds = (acceptedRecord.merged_job_ids ?? []).filter(
      (turnId) => turnId
        && turnId !== incoming.turn_id
        && !mergedTurnIds.has(turnId),
    );
    for (const turnId of newlyMergedIds) {
      mergedTurnIds.add(turnId);
      changed = true;
    }
    for (const turnId of newlyMergedIds) {
      if (Object.prototype.hasOwnProperty.call(turnsById, turnId)) {
        delete turnsById[turnId];
      }
      delete summaryTurnsById[turnId];
      detailAccessOrder = detailAccessOrder.filter((id) => id !== turnId);
    }
  }

  if (!changed) {
    return historyRevisionChanged
      ? { ...timeline, historyViewRevision }
      : timeline;
  }
  return compactTurnDetails({
    ...timeline,
    historyViewRevision,
    turnsById,
    summaryTurnsById,
    detailAccessOrder,
    orderedTurnIds: sortedTurnIds(turnsById),
    mergedTurnIds: [...mergedTurnIds],
    loadingDetailIds: timeline.loadingDetailIds.filter(
      (turnId) => !mergedTurnIds.has(turnId),
    ),
    invalidatedTurnIds: timeline.invalidatedTurnIds.filter(
      (turnId) => !mergedTurnIds.has(turnId),
    ),
  });
}

function compactTurnDetails(
  timeline: SessionTurnTimeline,
  maxDetailCount = TURN_DETAIL_CACHE_MAX_COUNT,
  maxProjectionUnits = TURN_DETAIL_CACHE_MAX_PROJECTION_UNITS,
): SessionTurnTimeline {
  const detailEntries: { turnId: string; projectionUnits: number }[] = [];
  let totalProjectionUnits = 0;
  for (const turnId of timeline.detailAccessOrder) {
    const detail = timeline.turnsById[turnId];
    if (!isTurnDetail(detail)) continue;
    const projectionUnits = projectionRichness(detail);
    detailEntries.push({ turnId, projectionUnits });
    totalProjectionUnits += projectionUnits;
  }
  let retainedDetailCount = detailEntries.length;
  let turnsById = timeline.turnsById;
  const retainedDetailIds: string[] = [];
  let changed = detailEntries.length !== timeline.detailAccessOrder.length;

  for (const { turnId, projectionUnits } of detailEntries) {
    if (
      retainedDetailCount <= maxDetailCount
      && totalProjectionUnits <= maxProjectionUnits
    ) {
      retainedDetailIds.push(turnId);
      continue;
    }
    const detail = turnsById[turnId];
    const summary = timeline.summaryTurnsById[turnId];
    if (
      !detail
      || !summary
      || !isTurnDetail(detail)
      || summary.revision !== detail.revision
      || summary.source_view_id !== detail.source_view_id
      || summary.source_view_revision !== detail.source_view_revision
    ) {
      retainedDetailIds.push(turnId);
      continue;
    }
    if (turnsById === timeline.turnsById) turnsById = { ...turnsById };
    turnsById[turnId] = summary;
    totalProjectionUnits -= projectionUnits;
    retainedDetailCount -= 1;
    changed = true;
  }

  if (!changed) return timeline;
  return {
    ...timeline,
    turnsById,
    detailAccessOrder: retainedDetailIds,
  };
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
    loadingBefore: false,
    loadingAfter: false,
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
  if (!bootstrap.thread_id) throw new Error("Turn bootstrap 缺少精确 thread owner");
  if (timeline.threadId !== null && timeline.threadId !== bootstrap.thread_id) {
    throw new Error("Turn bootstrap 的 thread owner 与已有时间线不一致");
  }
  let next = replaceProjection(timeline, bootstrap.projection_epoch);
  const preserveCachedWindow = next === timeline && (
    timeline.orderedTurnIds.length > 0 || timeline.internalExecutions.length > 0
  );
  const preserveBeforeWindow = preserveCachedWindow
    && (next.beforeCursor !== null || bootstrap.older_cursor === null);
  const projectionState = bootstrap.projection_state ?? "ready";
  next = {
    ...next,
    threadId: bootstrap.thread_id,
    phase: projectionState === "ready" ? "ready" : "bootstrapping",
    activeJobs: bootstrap.active_jobs ?? [],
    // bootstrap 只描述最新 Turn。重新进入已经加载过的会话时，不能把
    // 用户之前加载出的窗口和 before/after 游标抹掉，否则切回会话会
    // 突然退回尾部，看起来像历史位置丢失。
    beforeCursor: preserveBeforeWindow
      ? next.beforeCursor
      : bootstrap.older_cursor ?? null,
    afterCursor: preserveCachedWindow ? next.afterCursor : null,
    hasBefore: preserveBeforeWindow
      ? next.hasBefore
      : Boolean(bootstrap.older_cursor),
    hasAfter: preserveCachedWindow ? next.hasAfter : false,
    loadingBefore: false,
    loadingAfter: false,
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
  const next = upsertTurns(timeline, page.items);
  return {
    ...next,
    phase: "ready",
    projectionEpoch: page.projection_epoch,
    projectionState: "ready",
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
  const receivedIds = new Set<string>();
  for (const turn of batch.items) {
    receivedIds.add(turn.turn_id);
  }
  const freshItems = timeline.historyViewRevision === null
    ? batch.items
    : batch.items.filter((turn) =>
        turn.source_view_revision >= timeline.historyViewRevision!
      );
  const freshIds = new Set(freshItems.map((turn) => turn.turn_id));
  // 成功的详情响应证明这些 Turn 仍属于当前投影；允许它们从一次旧的
  // 404/失效标记中复活。先按快照 revision 排除迟到响应，避免旧详情撤销失效标记。
  const revalidatedTimeline = timeline.invalidatedTurnIds.some((turnId) =>
    freshIds.has(turnId)
  )
    ? {
        ...timeline,
        invalidatedTurnIds: timeline.invalidatedTurnIds.filter(
          (turnId) => !freshIds.has(turnId),
        ),
      }
    : timeline;
  const next = upsertTurns(revalidatedTimeline, freshItems);
  return {
    ...next,
    projectionEpoch: batch.projection_epoch,
    loadingDetailIds: next.loadingDetailIds.filter(
      (turnId) => !receivedIds.has(turnId),
    ),
    error: null,
  };
}

function releaseHistoryPageLoading(
  timeline: SessionTurnTimeline,
  direction: "before" | "after" | "around",
): SessionTurnTimeline {
  const loadingBefore = direction === "after" ? timeline.loadingBefore : false;
  const loadingAfter = direction === "before" ? timeline.loadingAfter : false;
  if (
    loadingBefore === timeline.loadingBefore
    && loadingAfter === timeline.loadingAfter
  ) {
    return timeline;
  }
  return { ...timeline, loadingBefore, loadingAfter };
}

export function applyTurnHistoryPage(
  timeline: SessionTurnTimeline,
  page: TurnHistoryPage,
  direction: "before" | "after" | "around" = "before",
): SessionTurnTimeline {
  requireMatchingEpoch(timeline, page.projection_epoch);
  if (!page.thread_id) throw new Error("Turn history 缺少精确 thread owner");
  if (timeline.threadId !== null && timeline.threadId !== page.thread_id) {
    throw new Error("Turn history 的 thread owner 与已有时间线不一致");
  }
  const hasSourceViewId = typeof page.source_view_id === "string";
  const hasSourceViewRevision = typeof page.source_view_revision === "number";
  if (hasSourceViewId !== hasSourceViewRevision) {
    throw new Error("Turn history 必须同时携带 source view/revision");
  }
  const hasPageBinding = hasSourceViewId && hasSourceViewRevision;
  if (hasPageBinding && !page.source_view_id) {
    throw new Error("Turn history source view 不能为空");
  }
  const hasMaterializedHistory = page.items.length > 0
    || (page.summaries?.length ?? 0) > 0
    || (page.internal_executions?.length ?? 0) > 0;
  if (!hasPageBinding && hasMaterializedHistory) {
    throw new Error("已物化 Turn history 缺少 source view/revision");
  }
  if (
    timeline.historyViewRevision !== null
    && (
      !hasPageBinding
      || page.source_view_revision! < timeline.historyViewRevision
    )
  ) {
    return releaseHistoryPageLoading(timeline, direction);
  }
  const pageTimeline: SessionTurnTimeline = {
    ...timeline,
    threadId: page.thread_id,
    historyViewRevision: hasPageBinding
      ? page.source_view_revision!
      : timeline.historyViewRevision,
  };
  const withSummaries = upsertTurns(pageTimeline, page.summaries ?? []);
  const next = upsertTurns(withSummaries, page.items);
  const internalExecutions = new Map(
    next.internalExecutions.map((execution) => [execution.execution_id, execution]),
  );
  const displayItemExecutions = new Map<string, string>();
  for (const execution of next.internalExecutions) {
    for (const part of execution.display_parts) {
      const owner = displayItemExecutions.get(part.item_id);
      if (owner !== undefined && owner !== execution.execution_id) {
        throw new Error(
          `history projection integrity: display item ${part.item_id} 属于多个 execution`,
        );
      }
      displayItemExecutions.set(part.item_id, execution.execution_id);
    }
  }
  for (const execution of page.internal_executions ?? []) {
    const previous = internalExecutions.get(execution.execution_id);
    const displayParts = new Map(
      (previous?.display_parts ?? []).map((part) => [part.item_id, part]),
    );
    for (const part of execution.display_parts) {
      const owner = displayItemExecutions.get(part.item_id);
      if (owner !== undefined && owner !== execution.execution_id) {
        throw new Error(
          `history projection integrity: display item ${part.item_id} 属于多个 execution`,
        );
      }
      displayItemExecutions.set(part.item_id, execution.execution_id);
      const previousPart = displayParts.get(part.item_id);
      if (
        previousPart !== undefined
        && (
          previousPart.item_sequence !== part.item_sequence
          || previousPart.logical_item_ordinal !== part.logical_item_ordinal
        )
      ) {
        throw new Error(
          `history projection integrity: 同一 display item 的 item_sequence 或 membership ordinal 冲突: ${part.item_id}`,
        );
      }
      displayParts.set(part.item_id, part);
    }
    const responseParts = new Map(
      (previous?.response_parts ?? []).map((part) => [part.part_id, part]),
    );
    for (const part of execution.response_parts) {
      const previousPart = responseParts.get(part.part_id);
      if (
        previousPart !== undefined
        && (
          previousPart.source.item_id !== part.source.item_id
          || previousPart.source.item_sequence !== part.source.item_sequence
          || previousPart.source.logical_item_ordinal
            !== part.source.logical_item_ordinal
          || previousPart.source.part_ordinal !== part.source.part_ordinal
        )
      ) {
        throw new Error(
          `history projection integrity: 同一 response part 的 canonical 坐标冲突: ${part.part_id}`,
        );
      }
      responseParts.set(part.part_id, part);
    }
    const orderedResponseParts = [...responseParts.values()];
    orderedResponseParts.sort(compareInternalResponseParts);
    internalExecutions.set(execution.execution_id, {
      ...execution,
      display_parts: [...displayParts.values()].sort((left, right) =>
        left.logical_item_ordinal - right.logical_item_ordinal,
      ),
      response_parts: orderedResponseParts,
    });
  }
  const nextInternalExecutions = [...internalExecutions.values()];
  if (direction === "around") {
    return {
      ...next,
      internalExecutions: nextInternalExecutions,
      phase: "ready",
      beforeCursor: page.before_cursor ?? null,
      afterCursor: page.after_cursor ?? null,
      hasBefore: page.has_before ?? Boolean(page.before_cursor),
      hasAfter: page.has_after ?? Boolean(page.after_cursor),
      loadingBefore: false,
      loadingAfter: false,
      projectionEpoch: page.projection_epoch,
      projectionState: "ready",
      error: null,
    };
  }
  const isBefore = direction === "before";
  const cursor = isBefore
    ? page.before_cursor ?? page.next_cursor ?? null
    : page.after_cursor ?? page.next_cursor ?? null;
  const hasMore = isBefore
    ? page.has_before ?? page.has_more ?? Boolean(cursor)
    : page.has_after ?? page.has_more ?? Boolean(cursor);
  return {
    ...next,
    internalExecutions: nextInternalExecutions,
    phase: "ready",
    beforeCursor: isBefore ? cursor : timeline.beforeCursor,
    afterCursor: isBefore ? timeline.afterCursor : cursor,
    hasBefore: isBefore ? hasMore : timeline.hasBefore,
    hasAfter: isBefore ? timeline.hasAfter : hasMore,
    projectionEpoch: page.projection_epoch,
    projectionState: "ready",
    loadingBefore: isBefore ? false : timeline.loadingBefore,
    loadingAfter: isBefore ? timeline.loadingAfter : false,
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

/**
 * 从当前视图移除已经失效的 Turn，同时留下不可见标记，防止旧的异步响应再次把它加入时间线。
 * 新的 projection epoch 会由 replaceProjection 创建全新的标记集合。
 */
export function dropTurn(
  timeline: SessionTurnTimeline,
  turnId: string,
): SessionTurnTimeline {
  if (!turnId) return timeline;
  const hadTurn = Object.prototype.hasOwnProperty.call(timeline.turnsById, turnId);
  const alreadyInvalidated = timeline.invalidatedTurnIds.includes(turnId);
  if (!hadTurn && alreadyInvalidated) return timeline;

  const turnsById = { ...timeline.turnsById };
  const summaryTurnsById = { ...timeline.summaryTurnsById };
  delete turnsById[turnId];
  delete summaryTurnsById[turnId];
  return {
    ...timeline,
    turnsById,
    summaryTurnsById,
    orderedTurnIds: timeline.orderedTurnIds.filter((id) => id !== turnId),
    detailAccessOrder: timeline.detailAccessOrder.filter((id) => id !== turnId),
    loadingDetailIds: timeline.loadingDetailIds.filter((id) => id !== turnId),
    invalidatedTurnIds: alreadyInvalidated
      ? timeline.invalidatedTurnIds
      : [...timeline.invalidatedTurnIds, turnId],
    error: null,
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
    loadingBefore: false,
    loadingAfter: false,
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
  for (const [key, cachedTimeline] of next.entries()) {
    if (key !== scopeKey) {
      next.set(key, compactTurnDetails(cachedTimeline, 0, 0));
    }
  }
  next.delete(scopeKey);
  next.set(scopeKey, compactTurnDetails(timeline));
  while (next.size > TURN_TIMELINE_CACHE_LIMIT) {
    const oldestKey = next.keys().next().value;
    if (typeof oldestKey !== "string") {
      break;
    }
    next.delete(oldestKey);
  }
  return next;
}
