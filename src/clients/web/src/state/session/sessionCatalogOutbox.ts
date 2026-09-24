import {
  NAVIGATION_MUTATION_TERMINAL_STATES,
  type NavigationMutationKind,
  type NavigationMutationState,
  type SessionCatalogOperationIntent,
  type SessionCatalogOperationReceipt,
} from "../../api/session/sessionCatalogOperations";

/**
 * 会话目录本地 outbox 的纯状态机（OpenSpec 8.1-H）。
 *
 * 目录编辑采用「已提交 catalog 基线 + 本地有序命令重放」：本模块只负责 pending
 * 命令自身的状态推进，不碰后端权威镜像、分页 cursor/total 或任何 React 状态。
 * 状态推进严格单向：
 *
 * - `pending_local`：用户操作同步建立的意图，尚未确认本地持久化；
 * - `persisted`：已按序写入本地 outbox，**只有进入该状态才允许派发后端入队**；
 * - `accepted`：已取得 202 durable acceptance（绝不等于目录已改变）；
 * - `unknown`：HTTP 超时/断线/202 丢失，保留原 ID 查询或重试，不得回退或换 ID；
 * - 终态 `committed|rejected|cancelled|dependency_failed`：由对账写入。
 *
 * 本地持久化失败不走状态推进：调用方必须移除该意图及其传递依赖，从 confirmed
 * 基线重新投影（见 `rollbackCatalogOutboxIntents`）。
 */

export type CatalogOutboxLocalState = "pending_local" | "persisted" | "accepted" | "unknown";

/** 本地状态与后端 operation 状态的并集；后端状态来自协议，本地不重复定义。 */
export type CatalogOutboxOperationState = CatalogOutboxLocalState | NavigationMutationState;

export interface CatalogOutboxPartition {
  /** 稳定 Gateway 身份：本地 Gateway 用其监听端口，远程 Gateway 用其 gateway_id。 */
  gatewayId: string;
  workspaceId: string;
  /** 认证主体：Gateway 用户 user_id，游客为 `guest`。 */
  principal: string;
}

/** 用户操作同步建立的意图；字段是后端 wire intent 的客户端侧前置形态。 */
export interface CatalogOutboxIntent {
  kind: NavigationMutationKind;
  /** rename/move/delete 的稳定目标 node；create_folder 不允许给。 */
  targetNodeId?: string | null;
  /** move 的目标父节点稳定 ID；移到根为 null。 */
  parentNodeId?: string | null;
  /** move 到本 outbox 内 pending 新建的 Folder 时，引用创建它的 operation。 */
  parentCreatedByOperationId?: string | null;
  name?: string | null;
  recursive?: boolean;
}

export interface CatalogOutboxOptions {
  baseCatalogRevision: number;
  /** rename/move 的执行期 CAS 前置，来自 confirmed 快照的 node revision。 */
  expectedRevision?: number | null;
  dependsOn?: readonly string[];
}

export interface CatalogOutboxOperation {
  client_operation_id: string;
  client_sequence: number;
  kind: NavigationMutationKind;
  target_node_id: string | null;
  parent_node_id: string | null;
  created_by_operation_id: string | null;
  name: string | null;
  recursive: boolean;
  depends_on: string[];
  base_catalog_revision: number;
  expected_revision: number | null;
  state: CatalogOutboxOperationState;
  /** 服务端最近一次报告的状态；本地 `accepted` 与它是两件事（202 ≠ 目录已改变）。 */
  server_state: NavigationMutationState | null;
  /** 新建 Folder 的本地临时引用：拿到 canonical ID 前用于投影，之后必须清空。 */
  client_ref: string | null;
  /** 后端分配的 canonical Folder ID（create_folder 才有）。 */
  created_node_id: string | null;
  queue_seq: number | null;
  receipt_revision: number | null;
  error_code: string | null;
  error_detail: string | null;
}

export interface CatalogOutbox {
  partition: CatalogOutboxPartition;
  /** 单调递增的本地序号，用作后端 `client_sequence`。 */
  next_client_sequence: number;
  operations: CatalogOutboxOperation[];
}

/**
 * outbox 分区键的唯一实现：按稳定 gateway/workspace/principal 三元组隔离。
 *
 * 同一 Gateway 的不同工作区、或同一工作区的不同认证主体之间不得共享 outbox 条目。
 * 用 `\u0000` 分隔，避免任何一段自身含分隔符时与相邻分区串键。
 */
export function catalogOutboxPartitionKey(
  partition: CatalogOutboxPartition,
): string {
  const fields: ReadonlyArray<readonly [string, string]> = [
    ["gatewayId", partition.gatewayId],
    ["workspaceId", partition.workspaceId],
    ["principal", partition.principal],
  ];
  for (const [field, value] of fields) {
    if (typeof value !== "string" || value === "") {
      throw new Error(
        `outbox 分区键的 ${field} 必须是非空字符串: ${JSON.stringify(value)}`,
      );
    }
  }
  return fields.map(([, value]) => value).join("\u0000");
}

export function createCatalogOutbox(partition: CatalogOutboxPartition): CatalogOutbox {
  catalogOutboxPartitionKey(partition);
  return { partition, next_client_sequence: 1, operations: [] };
}

/** 终态判定的唯一实现：终态集合来自后端协议，本地不得再写第二份。 */
export function catalogOutboxOperationIsTerminal(
  operation: CatalogOutboxOperation,
): boolean {
  return (NAVIGATION_MUTATION_TERMINAL_STATES as readonly string[]).includes(operation.state);
}

/** 按 `client_sequence` 排序的 outbox 视图：持久化与入队都必须按此顺序。 */
export function orderedCatalogOutboxOperations(
  outbox: CatalogOutbox,
): CatalogOutboxOperation[] {
  return [...outbox.operations].sort(
    (left, right) => left.client_sequence - right.client_sequence,
  );
}

/** 尚未终态、仍需投影与对账的 operation。 */
export function unsettledCatalogOutboxOperations(
  outbox: CatalogOutbox,
): CatalogOutboxOperation[] {
  return orderedCatalogOutboxOperations(outbox).filter(
    (operation) => !catalogOutboxOperationIsTerminal(operation),
  );
}

function requireKnownOperationReference(
  knownIds: ReadonlySet<string>,
  clientOperationId: string,
  field: string,
  referencedId: string,
): void {
  if (referencedId === clientOperationId) {
    throw new Error(`operation 的 ${field} 不能引用自身: ${clientOperationId}`);
  }
  if (!knownIds.has(referencedId)) {
    throw new Error(
      `operation 的 ${field} 不在本 outbox 内，无法建立因果顺序: `
      + `${clientOperationId} -> ${referencedId}`,
    );
  }
}

/**
 * 同步建立一条 pending intent 并立即返回新 outbox（不改动入参）。
 *
 * 依赖引用必须指向本 outbox 内已存在的 operation；悬空依赖属于契约被破坏，必须在
 * 本地响亮失败，不能留到后端排队期才发现。
 */
export function addCatalogOutboxIntent(
  outbox: CatalogOutbox,
  clientOperationId: string,
  intent: CatalogOutboxIntent,
  options: CatalogOutboxOptions,
): CatalogOutbox {
  if (typeof clientOperationId !== "string" || clientOperationId === "") {
    throw new Error(
      `client_operation_id 必须是非空字符串: ${JSON.stringify(clientOperationId)}`,
    );
  }
  if (!Number.isInteger(options.baseCatalogRevision) || options.baseCatalogRevision < 0) {
    throw new Error(`base_catalog_revision 必须是非负整数: ${options.baseCatalogRevision}`);
  }
  if (outbox.operations.some((item) => item.client_operation_id === clientOperationId)) {
    throw new Error(`outbox 已存在同 ID operation: ${clientOperationId}`);
  }
  const knownIds = new Set(outbox.operations.map((item) => item.client_operation_id));
  const dependsOn = [...(options.dependsOn ?? [])];
  for (const dependencyId of dependsOn) {
    requireKnownOperationReference(knownIds, clientOperationId, "depends_on", dependencyId);
  }
  const createdBy = intent.parentCreatedByOperationId ?? null;
  if (createdBy !== null) {
    requireKnownOperationReference(
      knownIds,
      clientOperationId,
      "parent_created_by_operation_id",
      createdBy,
    );
    if (intent.kind !== "move_node") {
      throw new Error("只有 move_node 可以用 created_by_operation_id 引用 pending 新建的父节点");
    }
    if (intent.parentNodeId) {
      throw new Error("move_node 不能同时给出 parent_node_id 与 parentCreatedByOperationId");
    }
  }
  if (intent.kind === "create_folder") {
    if (intent.targetNodeId) {
      throw new Error("create_folder 不接受 target_node_id，必须用 client_ref 投影未确认 Folder");
    }
    if (!intent.name) {
      throw new Error("create_folder 必须给出非空 name");
    }
  } else {
    if (!intent.targetNodeId) {
      throw new Error(`${intent.kind} 必须给出 target_node_id`);
    }
    if (intent.kind === "rename_node" || intent.kind === "move_node") {
      if (!Number.isInteger(options.expectedRevision ?? null)) {
        throw new Error(`${intent.kind} 必须给出目标 node 的 expected_revision`);
      }
    }
  }
  const operation: CatalogOutboxOperation = {
    client_operation_id: clientOperationId,
    client_sequence: outbox.next_client_sequence,
    kind: intent.kind,
    target_node_id: intent.targetNodeId ?? null,
    parent_node_id: intent.parentNodeId ?? null,
    created_by_operation_id: createdBy,
    name: intent.name ?? null,
    recursive: intent.recursive ?? false,
    depends_on: dependsOn,
    base_catalog_revision: options.baseCatalogRevision,
    expected_revision: options.expectedRevision ?? null,
    state: "pending_local",
    server_state: null,
    // create_folder 的本地投影引用就是它自己的 operation ID：全局唯一，且绝不等同于
    // canonical node ID（后端分配后写入 created_node_id 并清空 client_ref）。
    client_ref: intent.kind === "create_folder" ? clientOperationId : null,
    created_node_id: null,
    queue_seq: null,
    receipt_revision: null,
    error_code: null,
    error_detail: null,
  };
  return {
    ...outbox,
    next_client_sequence: outbox.next_client_sequence + 1,
    operations: [...outbox.operations, operation],
  };
}

function updateCatalogOutboxOperation(
  outbox: CatalogOutbox,
  operationId: string,
  update: (operation: CatalogOutboxOperation) => CatalogOutboxOperation,
): CatalogOutbox {
  const index = outbox.operations.findIndex(
    (operation) => operation.client_operation_id === operationId,
  );
  if (index < 0) {
    throw new Error(`outbox 不存在该 operation: ${operationId}`);
  }
  const operations = [...outbox.operations];
  operations[index] = update(operations[index]);
  return { ...outbox, operations };
}

/**
 * 本地持久化成功的唯一推进：`pending_local → persisted`。
 *
 * 只有 persisted 的 operation 才允许进入入队批次；重复推进是幂等的。
 */
export function markCatalogOutboxOperationPersisted(
  outbox: CatalogOutbox,
  operationId: string,
): CatalogOutbox {
  return updateCatalogOutboxOperation(outbox, operationId, (operation) =>
    operation.state === "pending_local" ? { ...operation, state: "persisted" } : operation,
  );
}

/** 失败/撤销的传播方向：给定 operation 集合，向依赖它们的后继闭包扩散。 */
function collectCatalogOutboxDependentClosure(
  outbox: CatalogOutbox,
  seeds: readonly string[],
): string[] {
  const ordered = orderedCatalogOutboxOperations(outbox);
  const doomed = new Set(seeds);
  for (const operation of ordered) {
    if (doomed.has(operation.client_operation_id)) continue;
    const dependsOnDoomed = operation.depends_on.some((dependencyId) => doomed.has(dependencyId))
      || (operation.created_by_operation_id !== null
        && doomed.has(operation.created_by_operation_id));
    if (dependsOnDoomed) doomed.add(operation.client_operation_id);
  }
  return ordered
    .map((operation) => operation.client_operation_id)
    .filter((operationId) => doomed.has(operationId));
}

/**
 * 本地持久化失败的唯一回退：移除该意图及其**传递依赖**，从 confirmed 基线重新投影。
 *
 * 未持久化的意图绝不允许派发后端入队，因此必须整体撤销，不能留下半个依赖链。
 */
export function rollbackCatalogOutboxIntents(
  outbox: CatalogOutbox,
  removedOperationIds: readonly string[],
): { outbox: CatalogOutbox; removed_operation_ids: string[] } {
  const removed = new Set(collectCatalogOutboxDependentClosure(outbox, removedOperationIds));
  return {
    outbox: {
      ...outbox,
      operations: outbox.operations.filter(
        (operation) => !removed.has(operation.client_operation_id),
      ),
    },
    removed_operation_ids: [...removed],
  };
}

/**
 * 下一步可入队的批次：按 `client_sequence` 有序，且依赖必须在同批之内或已取得
 * durable acceptance（`accepted` 及之后状态）。
 *
 * 依赖仍停留在 `pending_local`/`persisted` 时该 operation 本轮跳过，等依赖先入队，
 * 保证后端看到的因果顺序与本地一致。
 */
export function planCatalogOutboxBatch(
  outbox: CatalogOutbox,
  options: { maxBatchSize: number; retryableOperationIds?: readonly string[] },
): CatalogOutboxOperation[] {
  if (!Number.isInteger(options.maxBatchSize) || options.maxBatchSize < 1) {
    throw new Error(`入队批次上限必须是正整数: ${options.maxBatchSize}`);
  }
  const ordered = orderedCatalogOutboxOperations(outbox);
  // `unknown` 只能在服务端明确回答「不认识该 ID」后才允许按**同一 ID** 重试；
  // 未确认前一律保留 pending，不得当作可入队或可满足前置。
  const retryable = new Set(options.retryableOperationIds ?? []);
  // 依赖只有拿到 durable acceptance 或已 committed 才可放行：`unknown` 表示我们
  // 并不知道后端是否已接受，此时绝不能把它当作前置已满足，否则会发出悬空依赖。
  const accepted = new Set(
    ordered
      .filter((operation) => operation.state === "accepted"
        || operation.state === "committed")
      .map((operation) => operation.client_operation_id),
  );
  const batch: CatalogOutboxOperation[] = [];
  const batchDependable = new Set<string>();
  for (const operation of ordered) {
    const includable = operation.state === "persisted"
      || (operation.state === "unknown" && retryable.has(operation.client_operation_id));
    if (!includable) continue;
    if (batch.length >= options.maxBatchSize) break;
    const ready = operation.depends_on.every(
      (dependencyId) => accepted.has(dependencyId) || batchDependable.has(dependencyId),
    );
    if (!ready) continue;
    batch.push(operation);
    batchDependable.add(operation.client_operation_id);
  }
  return batch;
}

/** 把批次内 operation 转为入队 wire intent；依赖只在同批内保留。 */
export function catalogOutboxBatchToIntents(
  batch: readonly CatalogOutboxOperation[],
): SessionCatalogOperationIntent[] {
  const batchIds = new Set(batch.map((operation) => operation.client_operation_id));
  return batch.map((operation) => {
    const intent: SessionCatalogOperationIntent = {
      client_operation_id: operation.client_operation_id,
      client_sequence: operation.client_sequence,
      kind: operation.kind,
      base_catalog_revision: operation.base_catalog_revision,
      depends_on: operation.depends_on.filter((dependencyId) => batchIds.has(dependencyId)),
    };
    if (operation.expected_revision !== null) intent.expected_revision = operation.expected_revision;
    if (operation.target_node_id !== null) intent.target_node_id = operation.target_node_id;
    if (operation.created_by_operation_id !== null) {
      intent.created_by_operation_id = operation.created_by_operation_id;
    } else if (operation.parent_node_id !== null) {
      intent.parent_node_id = operation.parent_node_id;
    }
    if (operation.kind === "move_node" && operation.parent_node_id === null
      && operation.created_by_operation_id === null) {
      // 移到根必须显式给出 parent_node_id=null，后端据此区分「省略」与「移到根」。
      intent.parent_node_id = null;
    }
    if (operation.name !== null) intent.name = operation.name;
    if (operation.kind === "delete_folder") intent.recursive = operation.recursive;
    return intent;
  });
}

/**
 * 入队结果的唯一对账入口：把 receipt 的 durable 事实合并进 outbox。
 *
 * 状态推进规则由这一处统一决定，调用方不得各自解释 receipt：
 *
 * - 终态 receipt → 直接进入对应终态；
 * - 非终态 receipt 且本地仍是 `persisted`/`unknown` → 升级为 `accepted`：服务端能报出
 *   `queued|running` 就说明它确实已 durable 接受，`unknown` 由此得到确认；
 * - 其余非终态 receipt 只刷新 `server_state` 等事实，**绝不回退**本地已推进的状态。
 *
 * `created_node_id` 是 client_ref 解析成 canonical ID 的唯一依据，收到后必须清空
 * client_ref，避免投影里同时出现临时节点与真实节点。
 */
export function applyCatalogOutboxReceipts(
  outbox: CatalogOutbox,
  receipts: readonly SessionCatalogOperationReceipt[],
): CatalogOutbox {
  let next = outbox;
  for (const receipt of receipts) {
    const index = next.operations.findIndex(
      (operation) => operation.client_operation_id === receipt.operation_id,
    );
    if (index < 0) {
      throw new Error(`收到未登记 operation 的 receipt: ${receipt.operation_id}`);
    }
    const existing = next.operations[index];
    if (catalogOutboxOperationIsTerminal(existing)) continue;
    const operations = [...next.operations];
    operations[index] = {
      ...existing,
      state: nextCatalogOutboxState(existing.state, receipt.state),
      server_state: receipt.state,
      queue_seq: receipt.queue_seq,
      receipt_revision: receipt.receipt_revision,
      created_node_id: receipt.created_node_id ?? existing.created_node_id,
      client_ref: receipt.created_node_id ? null : existing.client_ref,
      error_code: receipt.error_code,
      error_detail: receipt.error_detail,
    };
    next = { ...next, operations };
  }
  return next;
}

/** 单步状态推进：终态优先，其次把已确认接受的本地状态升级为 accepted。 */
function nextCatalogOutboxState(
  current: CatalogOutboxOperationState,
  reported: NavigationMutationState,
): CatalogOutboxOperationState {
  if ((NAVIGATION_MUTATION_TERMINAL_STATES as readonly string[]).includes(reported)) {
    return reported;
  }
  if (current === "pending_local" || current === "persisted" || current === "unknown") {
    return "accepted";
  }
  return current;
}

/**
 * `unknown` 的唯一入口：HTTP 超时/断线/202 丢失时保留原 ID 与 preimage，
 * 绝不删除、绝不换 ID 重发。
 */
export function markCatalogOutboxOperationsUnknown(
  outbox: CatalogOutbox,
  operationIds: readonly string[],
): CatalogOutbox {
  let next = outbox;
  for (const operationId of operationIds) {
    next = updateCatalogOutboxOperation(next, operationId, (operation) =>
      operation.state === "persisted"
        ? { ...operation, state: "unknown" }
        : operation,
    );
  }
  return next;
}

export interface CatalogOutboxFailureResolution {
  outbox: CatalogOutbox;
  /** 被移除的失败 operation 及其传递依赖，供 UI 提示具体受影响 node。 */
  removed_operation_ids: string[];
  failed_operation_ids: string[];
}

/**
 * 明确拒绝（`rejected|cancelled|dependency_failed`）的唯一处理：移除失败 operation
 * 及传递依赖，保留仍有效的独立 pending 命令等待重放。
 *
 * 已 `committed` 的事实不得被撤销，也不得用整树逆补丁覆盖后续成功操作。
 */
export function resolveCatalogOutboxFailures(
  outbox: CatalogOutbox,
): CatalogOutboxFailureResolution {
  const failed = orderedCatalogOutboxOperations(outbox)
    .filter((operation) => operation.state === "rejected"
      || operation.state === "cancelled"
      || operation.state === "dependency_failed")
    .map((operation) => operation.client_operation_id);
  if (failed.length === 0) {
    return { outbox, removed_operation_ids: [], failed_operation_ids: [] };
  }
  const rollback = rollbackCatalogOutboxIntents(outbox, failed);
  return {
    outbox: rollback.outbox,
    removed_operation_ids: rollback.removed_operation_ids,
    failed_operation_ids: failed,
  };
}

/**
 * outbox 清理的唯一判据：operation 已终态，且其对账所用的 catalog revision 已被确认。
 *
 * 未确认终态的条目必须留在 outbox，刷新/重开后才不会丢命令；也不允许把未确认状态
 * 标记成功，或提供无法确认安全性的纯本地撤销。
 */
export function pruneReconciledCatalogOutbox(
  outbox: CatalogOutbox,
  reconciledCatalogRevision: number,
): CatalogOutbox {
  if (!Number.isInteger(reconciledCatalogRevision) || reconciledCatalogRevision < 0) {
    throw new Error(`对账 catalog revision 必须是非负整数: ${reconciledCatalogRevision}`);
  }
  return {
    ...outbox,
    operations: outbox.operations.filter((operation) => {
      if (!catalogOutboxOperationIsTerminal(operation)) return true;
      if (operation.receipt_revision === null) return true;
      return operation.receipt_revision > reconciledCatalogRevision;
    }),
  };
}
