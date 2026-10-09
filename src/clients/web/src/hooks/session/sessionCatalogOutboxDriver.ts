import { HttpRequestError } from "../../api/http";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import {
  enqueueSessionCatalogOperations,
  isNavigationBackpressureStatus,
  querySessionCatalogOperationStatus,
  type SessionCatalogEnqueueResult,
  type SessionCatalogOperationStatusPage,
  type SessionCatalogOperationReceipt,
} from "../../api/session/sessionCatalogOperations";
import {
  addCatalogOutboxIntent,
  assignCatalogOutboxOperationSequence,
  applyCatalogOutboxReceipts,
  catalogOutboxPartitionKey,
  markCatalogOutboxOperationPersisted,
  markCatalogOutboxOperationsUnknown,
  orderedCatalogOutboxOperations,
  planCatalogOutboxBatch,
  pruneReconciledCatalogOutbox,
  resolveCatalogOutboxFailures,
  rollbackCatalogOutboxIntents,
  unsettledCatalogOutboxOperations,
  catalogOutboxBatchToIntents,
  type CatalogOutbox,
  type CatalogOutboxIntent,
  type CatalogOutboxOptions,
  type CatalogOutboxPartition,
} from "../../state/session/sessionCatalogOutbox";
import {
  deleteCatalogOutboxOperations,
  loadCatalogOutbox,
  persistCatalogOutboxPendingOperation,
  type CatalogOutboxPersistencePort,
} from "../../state/session/sessionCatalogOutboxStore";
import { errorMessage } from "../../utils/errorMessage";

/**
 * 会话目录 outbox 的编排驱动（OpenSpec 8.1-H）。
 *
 * 唯一职责是把纯状态机、IndexedDB 持久层与目录写协议串成一条链路：
 *
 * 1. 用户操作 → 同步建立内存 pending 并立即投影（`onChange`）；
 * 2. 按 `client_sequence` 有序写入本地 outbox；**写入成功前绝不派发后端入队**；
 * 3. 写入失败 → 撤销该 intent 及传递依赖、显示错误，不派发；
 * 4. 写入成功后后台有序批量入队；202 只推进为 durable acceptance；
 * 5. 超时/断线/202 丢失 → 标 `unknown`，按同一 ID 查询/重试，不换 ID；
 * 6. 明确拒绝 → 重读权威状态，移除失败及传递依赖，重放仍独立的命令。
 *
 * 跨 tab 可见性通过同分区的持久 outbox 广播：其它 tab 收到通知后从 IndexedDB 重读，
 * 因此各 tab 看到的是同一份已持久化 pending 集合，不会各自造出重复 node。
 */

/**
 * 目录写协议的唯一 HTTP 适配口。生产绑定真实 API 客户端；测试注入确定性桩，
 * 不为「将来可能」预写多套实现或兼容分支。
 */
export interface SessionCatalogOperationsAdapter {
  enqueue(
    port: number,
    workspaceTarget: GatewayWorkspaceRequestTarget,
    intents: ReturnType<typeof catalogOutboxBatchToIntents>,
    signal?: AbortSignal,
  ): Promise<SessionCatalogEnqueueResult>;
  queryStatus(
    port: number,
    workspaceTarget: GatewayWorkspaceRequestTarget,
    operationIds: readonly string[],
    signal?: AbortSignal,
  ): Promise<SessionCatalogOperationStatusPage>;
}

const defaultAdapter: SessionCatalogOperationsAdapter = {
  enqueue: (port, workspaceId, intents, signal) =>
    enqueueSessionCatalogOperations(port, workspaceId, intents, { signal }),
  queryStatus: (port, workspaceId, operationIds, signal) =>
    querySessionCatalogOperationStatus(port, workspaceId, operationIds, { signal }),
};

export type CatalogOutboxDispatchOutcome =
  /** 本轮全部入队并收到 202 durable acceptance。 */
  | "accepted"
  /** 结果未知（超时/断线/背压）：保留原 ID，等待重新查询或重试。 */
  | "unknown"
  /** 已被后端明确拒绝：失败 operation 及传递依赖已移除。 */
  | "rejected"
  /** 本地持久化失败：intent 及传递依赖已撤销。 */
  | "persistence_failed"
  /** 本轮无可入队命令。 */
  | "idle";

export type CatalogOutboxFailureReconciler = (
  outbox: CatalogOutbox,
  catalogRevision: number,
  finalize: () => Promise<void>,
  signal: AbortSignal,
) => Promise<void>;

export interface CatalogOutboxDriverInput {
  port: number;
  /** Gateway 注册表中的路由 ID；与 partition.workspaceId 的后端身份分离。 */
  gatewayWorkspaceId: string;
  workspaceTarget: GatewayWorkspaceRequestTarget;
  partition: CatalogOutboxPartition;
  persistence: CatalogOutboxPersistencePort;
  adapter?: SessionCatalogOperationsAdapter;
  /** 失败闭包只在 confirmed owner 完成权威刷新与本地投影后清理。 */
  reconcileFailure?: CatalogOutboxFailureReconciler;
  /** 每次 outbox 变化（含同步 pending 与对账）都回调，供 UI 立即重投影。 */
  onChange?: (outbox: CatalogOutbox) => void;
  /** 跨 tab 通知通道；未提供时不广播，但本地流程完全一致。 */
  broadcast?: CatalogOutboxBroadcaster;
  /** 入队批次上限：有界批量的唯一来源。 */
  maxBatchSize?: number;
}

/** 跨 tab 变更广播的最小契约（生产由 BroadcastChannel 实现）。 */
export interface CatalogOutboxBroadcaster {
  post(partitionKey: string): void;
}

export interface CatalogOutboxDriver {
  /** 恢复该分区已持久化的 outbox（刷新/重开）。 */
  restore(): Promise<CatalogOutbox>;
  /** 来自其它 tab 的通知：从持久层重读，保持一致投影。 */
  reload(): Promise<CatalogOutbox>;
  current(): CatalogOutbox;
  /** 用户操作的唯一入口：同步 pending → 有序持久化 → 后台有序批量入队。 */
  applyIntent(
    clientOperationId: string,
    intent: CatalogOutboxIntent,
    options: CatalogOutboxOptions,
  ): Promise<CatalogOutboxDispatchOutcome>;
  /** 对未终态命令按精确 ID 查询服务端状态并对账。 */
  reconcile(): Promise<CatalogOutbox>;
  /** 身份切换时 fence 当前请求并禁止后续请求。 */
  deactivate(): void;
  /** 按已确认的 catalog revision 清理已完成对账的终态条目。 */
  prune(
    reconciledCatalogRevision: number,
    operationIds?: readonly string[],
  ): Promise<CatalogOutbox>;
}

const DEFAULT_MAX_BATCH_SIZE = 50;

interface CatalogOutboxFlushResult {
  outcome: CatalogOutboxDispatchOutcome;
  operationOutcomes: ReadonlyMap<string, CatalogOutboxDispatchOutcome>;
}

function isDefinitiveRejection(error: unknown): boolean {
  return error instanceof HttpRequestError
    && error.status >= 400
    && error.status < 500
    && !isNavigationBackpressureStatus(error.status);
}

/** 超时/断线/背压一律归 `unknown`：结果未知，保留 pending 原 ID 等重试。 */
function isUnknownOutcome(error: unknown): boolean {
  if (error instanceof HttpRequestError) {
    return isNavigationBackpressureStatus(error.status) || error.status >= 500;
  }
  // fetch 网络错误与请求超时都不是 HttpRequestError，同样属于结果未知。
  return true;
}

export function createSessionCatalogOutboxDriver(
  input: CatalogOutboxDriverInput,
): CatalogOutboxDriver {
  const adapter = input.adapter ?? defaultAdapter;
  const maxBatchSize = input.maxBatchSize ?? DEFAULT_MAX_BATCH_SIZE;
  const partitionKey = catalogOutboxPartitionKey(input.partition);
  let outbox: CatalogOutbox | null = null;
  // 服务端明确回答「不认识该 ID」的 unknown operation：允许按**同一 ID** 重试。
  // 没有这个确认前，unknown 一律保留 pending，不当可入队、不当依赖已满足。
  const retryableOperationIds = new Set<string>();
  // 同一分区同一时刻只允许一个 flush：并发 flush 会把同一批命令重复入队。
  let flushInFlight: Promise<CatalogOutboxFlushResult> | null = null;
  let reconciliationInFlight: Promise<CatalogOutbox> | null = null;
  // 本轮权威回执只活到 confirmed 对账成功；失败时阻止后续派发，不建立第二份 confirmed。
  let receiptsAwaitingReconciliation: {
    receipts: readonly SessionCatalogOperationReceipt[];
    catalogRevision: number;
  } | null = null;
  let active = true;
  const activeRequests = new Set<AbortController>();

  const assertActive = (): void => {
    if (!active) {
      throw new Error(`会话目录 outbox principal 已失效，停止派发: ${partitionKey}`);
    }
  };

  const withRequestSignal = async <T>(
    request: (signal: AbortSignal) => Promise<T>,
  ): Promise<T> => {
    assertActive();
    const controller = new AbortController();
    activeRequests.add(controller);
    try {
      const result = await request(controller.signal);
      assertActive();
      return result;
    } catch (error: unknown) {
      assertActive();
      throw error;
    } finally {
      activeRequests.delete(controller);
    }
  };

  const publish = (next: CatalogOutbox, broadcast: boolean): CatalogOutbox => {
    outbox = next;
    input.onChange?.(next);
    if (broadcast) input.broadcast?.post(partitionKey);
    return next;
  };

  const requireOutbox = (): CatalogOutbox => {
    if (outbox === null) {
      throw new Error("会话目录 outbox 尚未恢复，必须先调用 restore() 再操作");
    }
    return outbox;
  };

  /** 后台有序批量入队：一次一批，直到无可入队命令或遇到未知/拒绝结果。 */
  const flushOnce = async (): Promise<CatalogOutboxFlushResult> => {
    let outcome: CatalogOutboxDispatchOutcome = "idle";
    const operationOutcomes = new Map<string, CatalogOutboxDispatchOutcome>();
    for (;;) {
      assertActive();
      if (reconciliationInFlight) await reconciliationInFlight;
      if (receiptsAwaitingReconciliation
        || resolveCatalogOutboxFailures(requireOutbox()).removed_operation_ids.length > 0) {
        await reconcileInternal();
      }
      const batch = planCatalogOutboxBatch(requireOutbox(), {
        maxBatchSize,
        retryableOperationIds: [...retryableOperationIds],
      });
      if (batch.length === 0) return { outcome, operationOutcomes };
      const batchIds = batch.map((operation) => operation.client_operation_id);
      let result: SessionCatalogEnqueueResult;
      try {
        result = await withRequestSignal((signal) => adapter.enqueue(
          input.port,
          input.workspaceTarget,
          catalogOutboxBatchToIntents(batch),
          signal,
        ));
      } catch (error: unknown) {
        assertActive();
        if (isDefinitiveRejection(error)) {
          const operationIdsBeforeReconcile = requireOutbox().operations.map(
            (operation) => operation.client_operation_id,
          );
          await reconcileInternal();
          const remainingOperationIds = new Set(
            requireOutbox().operations.map((operation) => operation.client_operation_id),
          );
          for (const operationId of operationIdsBeforeReconcile) {
            if (!remainingOperationIds.has(operationId)) {
              operationOutcomes.set(operationId, "rejected");
            }
          }
          for (const operationId of batchIds) {
            if (!operationOutcomes.has(operationId)) {
              operationOutcomes.set(operationId, "idle");
            }
          }
          return { outcome: "rejected", operationOutcomes };
        }
        if (!isUnknownOutcome(error)) throw error;
        // 结果未知：只做 unknown 标记，绝不把 ID 当作可重试——重试必须等
        // reconcile() 从服务端确认「不认识该 ID」。
        for (const operationId of batchIds) retryableOperationIds.delete(operationId);
        publish(
          markCatalogOutboxOperationsUnknown(requireOutbox(), batchIds),
          true,
        );
        for (const operationId of batchIds) {
          operationOutcomes.set(operationId, "unknown");
        }
        return { outcome: "unknown", operationOutcomes };
      }
      for (const operationId of batchIds) retryableOperationIds.delete(operationId);
      // 入队可能返回原 operation 的 terminal tombstone，同样必须走权威对账后才交付结果。
      if (reconciliationInFlight) await reconciliationInFlight;
      const reported = applyCatalogOutboxReceipts(requireOutbox(), result.receipts);
      const failed = resolveCatalogOutboxFailures(reported);
      if (failed.removed_operation_ids.length > 0) {
        receiptsAwaitingReconciliation = { receipts: result.receipts, catalogRevision: 0 };
        await reconcileInternal();
      } else publish(reported, true);
      for (const operationId of batchIds) {
        operationOutcomes.set(operationId,
          failed.removed_operation_ids.includes(operationId) ? "rejected" : "accepted");
      }
      outcome = failed.removed_operation_ids.length > 0 ? "rejected" : "accepted";
    }
  };

  const flush = async (): Promise<CatalogOutboxFlushResult> => {
    if (flushInFlight !== null) return await flushInFlight;
    const run = flushOnce().finally(() => {
      if (flushInFlight === run) flushInFlight = null;
    });
    flushInFlight = run;
    return await run;
  };

  /** 所有 terminal 回执复用同一个 confirmed 对账入口。 */
  const reconcileReceipts = async (
    receipts: readonly SessionCatalogOperationReceipt[],
    catalogRevision: number,
  ): Promise<CatalogOutbox> => {
    const reported = applyCatalogOutboxReceipts(requireOutbox(), receipts);
    const resolution = resolveCatalogOutboxFailures(reported);
    if (resolution.removed_operation_ids.length === 0 && !receiptsAwaitingReconciliation) {
      return publish(reported, true);
    }
    receiptsAwaitingReconciliation = { receipts, catalogRevision };
    const failures = reported.operations.filter((operation) =>
      resolution.failed_operation_ids.includes(operation.client_operation_id))
      .map((operation) => `${operation.client_operation_id} (${operation.target_node_id ?? operation.client_ref ?? "目录"}, ${operation.state}): ${operation.error_detail ?? operation.error_code ?? "后端未提供失败详情"}`).join("；")
      || "失败闭包已持久清理，独立 pending 的 confirmed 投影尚未确认";
    const reconcileFailure = input.reconcileFailure;
    if (!reconcileFailure) {
      throw new Error("会话目录失败对账缺少 confirmed 分支刷新 owner，保留 pending 等待恢复: " + failures);
    }
    try {
      await withRequestSignal((signal) => reconcileFailure(
      reported,
      catalogRevision,
      async () => {
        assertActive();
        // 回执只合并一次；terminal 原记录保留到持久清理完成，失败可由同一 driver 重试。
        publish(applyCatalogOutboxReceipts(requireOutbox(), receipts), false);
        receiptsAwaitingReconciliation = { receipts: [], catalogRevision };
        const deletedOperationIds = new Set<string>();
        for (;;) {
          assertActive();
          const currentResolution = resolveCatalogOutboxFailures(requireOutbox());
          const idsToDelete = currentResolution.removed_operation_ids.filter(
            (id) => !deletedOperationIds.has(id),
          );
          if (idsToDelete.length === 0) {
            publish(currentResolution.outbox, deletedOperationIds.size > 0);
            return;
          }
          await deleteCatalogOutboxOperations(input.persistence, input.partition, idsToDelete);
          assertActive();
          for (const id of idsToDelete) deletedOperationIds.add(id);
          // delete 等待期间新增的依赖也走同一闸门；始终从当前 outbox 保留独立 intent。
        }
      },
      signal,
      ));
    } catch (error) {
      assertActive();
      throw new Error(`会话目录失败 (${failures})，confirmed 对账未完成: ${errorMessage(error)}`);
    }
    receiptsAwaitingReconciliation = null;
    return requireOutbox();
  };

  /** 未终态命令按精确 ID 查询；unknown 不回退，失败先完成 confirmed 对账。 */
  const reconcileOnce = async (): Promise<CatalogOutbox> => {
    assertActive();
    if (receiptsAwaitingReconciliation) {
      return await reconcileReceipts(
        receiptsAwaitingReconciliation.receipts, receiptsAwaitingReconciliation.catalogRevision,
      );
    }
    const tracked = unsettledCatalogOutboxOperations(requireOutbox()).filter(
      (operation) => operation.state !== "pending_local",
    );
    if (tracked.length === 0) return await reconcileReceipts([], 0);
    const page = await withRequestSignal((signal) => adapter.queryStatus(
      input.port, input.workspaceTarget,
      tracked.map((operation) => operation.client_operation_id), signal,
    ));
    for (const operationId of page.unknown_operation_ids) retryableOperationIds.add(operationId);
    for (const receipt of page.items) retryableOperationIds.delete(receipt.operation_id);
    return await reconcileReceipts(page.items, page.catalog_revision);
  };

  const reconcileInternal = async (): Promise<CatalogOutbox> => {
    if (reconciliationInFlight) return await reconciliationInFlight;
    const request = reconcileOnce().finally(() => {
      if (reconciliationInFlight === request) reconciliationInFlight = null;
    });
    reconciliationInFlight = request;
    return await request;
  };

  const flushOperation = async (
    operationId: string,
  ): Promise<CatalogOutboxDispatchOutcome> => {
    for (;;) {
      const operation = requireOutbox().operations.find(
        (candidate) => candidate.client_operation_id === operationId,
      );
      if (!operation) return "rejected";
      if (operation.state === "accepted" || operation.state === "committed") {
        return "accepted";
      }
      if (operation.state === "unknown") return "unknown";
      if (operation.state !== "persisted") return "idle";

      const result = await flush();
      const ownOutcome = result.operationOutcomes.get(operationId);
      if (ownOutcome !== undefined) return ownOutcome;

      const current = requireOutbox().operations.find(
        (candidate) => candidate.client_operation_id === operationId,
      );
      if (!current) return "rejected";
      if (current.state === "accepted" || current.state === "committed") {
        return "accepted";
      }
      if (current.state === "unknown") return "unknown";
      if (current.state !== "persisted") return "idle";

      const nextBatch = planCatalogOutboxBatch(requireOutbox(), {
        maxBatchSize,
        retryableOperationIds: [...retryableOperationIds],
      });
      if (!nextBatch.some((candidate) => candidate.client_operation_id === operationId)) {
        return "idle";
      }
      // 共享 flush 可能在本命令落盘前结束，也可能只处理了另一条被明确拒绝的命令。
      // 仅当本命令仍可入队时重新规划，避免把别的命令 outcome 传给它。
    }
  };

  return {
    async restore() {
      assertActive();
      const restored = await loadCatalogOutbox(input.persistence, input.partition);
      assertActive();
      return publish(restored, false);
    },
    async reload() {
      assertActive();
      const beforeLoad = requireOutbox();
      const restored = await loadCatalogOutbox(input.persistence, input.partition);
      assertActive();
      const current = requireOutbox();
      const currentIds = new Set(current.operations.map((operation) => operation.client_operation_id));
      const restoredIds = new Set(restored.operations.map((operation) => operation.client_operation_id));
      const mergedById = new Map(restored.operations.map((operation) => [operation.client_operation_id, operation]));
      // 读取期间本地已清理的条目不能复活；已收到的 receipt 也不能被磁盘 pending 覆盖。
      for (const operation of beforeLoad.operations) {
        if (!currentIds.has(operation.client_operation_id)) mergedById.delete(operation.client_operation_id);
      }
      for (const operation of current.operations) mergedById.set(operation.client_operation_id, operation);
      publish({ ...restored, operations: orderedCatalogOutboxOperations({
        ...restored, operations: [...mergedById.values()],
      }) }, false);
      const removedExternally = beforeLoad.operations.some((operation) =>
        currentIds.has(operation.client_operation_id)
        && operation.state !== "pending_local" && operation.state !== "committed"
        && !restoredIds.has(operation.client_operation_id));
      // 另一个 tab 删除磁盘行不代表当前 confirmed 已刷新；先保留原 ID 查询 terminal。
      if (removedExternally || receiptsAwaitingReconciliation) await reconcileInternal();
      return requireOutbox();
    },
    current: requireOutbox,
    async applyIntent(clientOperationId, intent, options) {
      assertActive();
      // 1. 同步建立 pending 并立即投影：交互绝不等待本地写入或后台入队。
      const withPending = addCatalogOutboxIntent(
        requireOutbox(),
        clientOperationId,
        intent,
        options,
      );
      publish(withPending, false);
      // 2. IndexedDB 原子分配 canonical sequence 并写入；失败必须撤销并报错。
      let insertedOperationId: string | null = null;
      try {
        const pending = withPending.operations.find(
          (operation) => operation.client_operation_id === clientOperationId,
        );
        if (!pending) throw new Error(`pending operation 丢失: ${clientOperationId}`);
        assertActive();
        const stored = await persistCatalogOutboxPendingOperation(
          input.persistence,
          input.partition,
          pending,
        );
        assertActive();
        insertedOperationId = stored.operation.client_operation_id;
        if (stored.operation.client_operation_id !== clientOperationId
          || stored.partition_key !== partitionKey) {
          throw new Error(`IndexedDB 返回了不匹配的 outbox operation: ${clientOperationId}`);
        }
        if (!requireOutbox().operations.some(
          (operation) => operation.client_operation_id === clientOperationId,
        )) {
          throw new Error(`该 operation 已随失败依赖回滚，未派发: ${clientOperationId}`);
        }
        const sequenced = assignCatalogOutboxOperationSequence(
          requireOutbox(),
          clientOperationId,
          stored.client_sequence,
        );
        publish(
          markCatalogOutboxOperationPersisted(sequenced, clientOperationId),
          true,
        );
      } catch (error: unknown) {
        if (!active) throw error;
        const beforeRollback = requireOutbox();
        const rollback = rollbackCatalogOutboxIntents(beforeRollback, [clientOperationId]);
        const removedOperationIds = new Set(rollback.removed_operation_ids);
        if (insertedOperationId !== null) {
          removedOperationIds.add(insertedOperationId);
        }
        publish(rollback.outbox, true);
        // IndexedDB insert 事务若失败不会留下半条 operation；但更早已提交的依赖项
        // 可能仍需随回滚闭包清除，因此按稳定 operation ID 清理。
        try {
          if (removedOperationIds.size > 0) {
            await deleteCatalogOutboxOperations(
              input.persistence,
              input.partition,
              [...removedOperationIds],
            );
          }
        } catch (cleanupError: unknown) {
          throw new Error(
            "会话目录 pending 变更未能本地持久化，且已落盘条目回滚也失败，请检查本地存储:"
            + ` 写入错误=${errorMessage(error)}`
            + `；回滚错误=${errorMessage(cleanupError)}`,
          );
        }
        throw new Error(
          "会话目录 pending 变更未能本地持久化，已撤销该操作及依赖: "
          + errorMessage(error),
        );
      }
      // 3. 持久化成功后才允许后台有序批量入队。
      // 返回值必须如实反映本条命令是否已入队：`flush` 可能因依赖未满足（前序
      // 命令仍 unknown/persisted）或批量上限而在本轮**一条都没入队**，此时命令
      // 的本地状态仍是 `persisted`，把它坍缩成 `accepted` 会与后端 durable
      // acceptance 脱钩（F2）。直接透传 flush 的 outcome 区分这两种情形。
      return await flushOperation(clientOperationId);
    },
    async reconcile() {
      assertActive();
      await reconcileInternal();
      assertActive();
      await flush();
      assertActive();
      return requireOutbox();
    },
    deactivate() {
      active = false;
      for (const controller of activeRequests) controller.abort();
    },
    async prune(reconciledCatalogRevision, operationIds) {
      assertActive();
      const before = requireOutbox();
      const next = pruneReconciledCatalogOutbox(
        before,
        reconciledCatalogRevision,
        (operationIds ?? before.operations.map((operation) => operation.client_operation_id))
          .filter((id) => before.operations.some((operation) =>
            operation.client_operation_id === id && operation.state === "committed")),
      );
      const prunedOperationIds = before.operations
        .filter((operation) => !next.operations.some(
          (kept) => kept.client_operation_id === operation.client_operation_id,
        ))
        .map((operation) => operation.client_operation_id);
      if (prunedOperationIds.length > 0) {
        await deleteCatalogOutboxOperations(
          input.persistence,
          input.partition,
          prunedOperationIds,
        );
      }
      assertActive();
      const prunedIds = new Set(prunedOperationIds);
      const current = requireOutbox();
      return publish({
        ...current,
        operations: current.operations.filter(
          (operation) => !prunedIds.has(operation.client_operation_id),
        ),
      }, prunedOperationIds.length > 0);
    },
  };
}

/**
 * 会话目录 outbox 的跨 tab 广播通道名。
 *
 * 只广播分区键，不广播命令体：接收方一律从 IndexedDB 重读，避免通道里出现两份
 * 可能分叉的 outbox 副本。
 */
export const CATALOG_OUTBOX_BROADCAST_CHANNEL = "boxteam-session-catalog-outbox";

/** 基于 BroadcastChannel 的生产广播实现；环境不支持时由调用方跳过。 */
export function createCatalogOutboxBroadcaster(
  channelName: string = CATALOG_OUTBOX_BROADCAST_CHANNEL,
): {
  broadcaster: CatalogOutboxBroadcaster;
  subscribe: (handler: (key: string) => void) => () => void;
  close: () => void;
} {
  if (typeof BroadcastChannel === "undefined") {
    throw new Error("当前运行环境没有 BroadcastChannel，无法建立跨 tab 的目录 outbox 通知");
  }
  const channel = new BroadcastChannel(channelName);
  return {
    broadcaster: { post: (partitionKey) => channel.postMessage({ partitionKey }) },
    subscribe: (handler) => {
      const listener = (event: MessageEvent<{ partitionKey?: unknown }>) => {
        const partitionKey = event.data?.partitionKey;
        if (typeof partitionKey === "string" && partitionKey !== "") handler(partitionKey);
      };
      channel.addEventListener("message", listener);
      return () => channel.removeEventListener("message", listener);
    },
    close: () => channel.close(),
  };
}
