import type {
  Session,
  SessionCatalogNode,
  SessionCatalogPage,
} from "../../types/backend";
import {
  catalogOutboxOperationIsTerminal,
  orderedCatalogOutboxOperations,
  type CatalogOutbox,
  type CatalogOutboxOperation,
  type CatalogOutboxOperationState,
} from "./sessionCatalogOutbox";

/**
 * 会话目录的纯函数投影（OpenSpec 8.1-H）：`confirmed 后端镜像 + pending 命令重放`。
 *
 * 投影只读取后端已提交节点与本地 outbox，产出展示态视图；它不修改 confirmed
 * 镜像、不调整后端 `cursor`/`total`/`revision`，也不写入任何权威状态。pending 变更
 * 只作为显式本地覆盖存在：同一 node 的源分支与目标分支绝不会同时出现，pending 新建
 * 的 Folder 在拿到 canonical ID 前只以 `client_ref` 出现。
 */

/** 投影视图中的单个节点；`pending_*` 字段非空即表示来自本地未确认意图。 */
export interface CatalogProjectedNode {
  node_id: string;
  kind: "folder" | "session";
  name: string;
  parent_node_id: string | null;
  session_id: string | null;
  folder_id: string | null;
  has_children: boolean;
  storage_relative_path: string | null;
  created_at: string | null;
  updated_at: string | null;
  session: Session | null;
  /** 非空表示该节点（或其父链）来自本地 pending 意图，UI 必须显式标注而不是 success。 */
  pending_state: CatalogOutboxOperationState | null;
  pending_operation_id: string | null;
  /** 未确认 Folder 的本地引用；拿到 canonical ID 后为 null。 */
  client_ref: string | null;
}

/** 投影后的分支视图：分页字段逐字来自后端 confirmed 页，绝不被 pending 改动。 */
export interface CatalogBranchProjection {
  revision: string;
  parent_node_id: string | null;
  items: CatalogProjectedNode[];
  cursor: string | null;
  total: number;
  consistency_warning: string | null;
  /** 本页中来自本地投影的 node ID，供 UI 单独标注。 */
  pending_node_ids: string[];
  /** 后端已提交但被本地 pending 移动/删除移出本分支的 node ID。 */
  locally_removed_node_ids: string[];
}

export interface CatalogProjectionInput {
  /** confirmed 后端镜像：所有已加载分页里的已提交节点。 */
  confirmedNodes: readonly SessionCatalogNode[];
  outbox: CatalogOutbox;
}

interface EffectiveNode {
  node: CatalogProjectedNode;
  confirmed: boolean;
  deleted: boolean;
  /** 本地新建 Folder 的创建 operation；用于把 client_ref 解析成 canonical ID。 */
  createdByOperationId: string | null;
}

/** 会话节点在投影中的唯一 ID：confirmed 用 node_id，本地新建用 canonical ID 或 client_ref。 */
function effectiveNodeId(operation: CatalogOutboxOperation): string {
  return operation.created_node_id ?? operation.client_ref ?? operation.client_operation_id;
}

function confirmedNodeToProjected(node: SessionCatalogNode): CatalogProjectedNode {
  return {
    node_id: node.node_id,
    kind: node.kind,
    name: node.name,
    parent_node_id: node.parent_node_id ?? null,
    session_id: node.session_id ?? null,
    folder_id: node.folder_id ?? null,
    has_children: node.has_children,
    storage_relative_path: node.storage_relative_path ?? null,
    created_at: node.created_at ?? null,
    updated_at: node.updated_at ?? null,
    session: node.session ?? null,
    pending_state: null,
    pending_operation_id: null,
    client_ref: null,
  };
}

/**
 * 把 confirmed 镜像与 outbox 按 `client_sequence` 重放成一张有效节点表。
 *
 * 这是本模块唯一的节点状态合成点：分支、面包屑与搜索都只消费它，避免三处各写
 * 一份 pending 归并而在某条路径上产生重复 node 或漏掉移动。
 */
function buildEffectiveNodes(input: CatalogProjectionInput): Map<string, EffectiveNode> {
  const table = new Map<string, EffectiveNode>();
  for (const node of input.confirmedNodes) {
    if (table.has(node.node_id)) {
      throw new Error(`confirmed 镜像出现重复 node_id: ${node.node_id}`);
    }
    table.set(node.node_id, {
      node: confirmedNodeToProjected(node),
      confirmed: true,
      deleted: false,
      createdByOperationId: null,
    });
  }

  const operations = orderedCatalogOutboxOperations(input.outbox);
  const resolution = new Map<string, string>();
  for (const operation of operations) {
    if (operation.kind === "create_folder") {
      resolution.set(operation.client_operation_id, effectiveNodeId(operation));
    }
  }
  // 依赖移动只能落在 canonical ID 或 client_ref 上：绝不允许把临时 ID 当作持久 node ID
  // 写进 confirmed 镜像，因此这里只解析成本地投影键。
  const resolveReference = (operation: CatalogOutboxOperation): string => {
    if (operation.created_by_operation_id === null) {
      throw new Error(
        `pending 引用缺少 created_by_operation_id: ${operation.client_operation_id}`,
      );
    }
    const resolved = resolution.get(operation.created_by_operation_id);
    if (resolved === undefined) {
      throw new Error(
        `pending 引用的创建 operation 不在 outbox 内: ${operation.created_by_operation_id}`,
      );
    }
    return resolved;
  };

  for (const operation of operations) {
    if (operation.kind === "create_folder") {
      const nodeId = effectiveNodeId(operation);
      const existing = table.get(nodeId);
      if (existing) {
        // 后端已把该 node 提交进 confirmed 镜像：沿用 confirmed 事实，只补 pending 标注。
        existing.node.pending_state = operation.state;
        existing.node.pending_operation_id = operation.client_operation_id;
        existing.createdByOperationId = operation.client_operation_id;
        continue;
      }
      table.set(nodeId, {
        node: {
          node_id: nodeId,
          kind: "folder",
          name: operation.name ?? "",
          parent_node_id: operation.parent_node_id,
          session_id: null,
          folder_id: null,
          has_children: false,
          storage_relative_path: null,
          created_at: null,
          updated_at: null,
          session: null,
          pending_state: operation.state,
          pending_operation_id: operation.client_operation_id,
          client_ref: operation.client_ref,
        },
        confirmed: false,
        deleted: false,
        createdByOperationId: operation.client_operation_id,
      });
      continue;
    }
    if (operation.target_node_id === null) continue;
    const target = table.get(operation.target_node_id);
    if (!target) {
      // 目标节点所在分支尚未加载：pending 只对已加载节点投影，绝不为未知节点造幽灵行。
      continue;
    }
    if (operation.kind === "rename_node") {
      target.node.name = operation.name ?? target.node.name;
    } else if (operation.kind === "move_node") {
      target.node.parent_node_id = operation.created_by_operation_id === null
        ? operation.parent_node_id
        : resolveReference(operation);
    } else {
      target.deleted = true;
    }
    if (!catalogOutboxOperationIsTerminal(operation)
      || operation.state === "dependency_failed"
      || operation.state === "rejected") {
      target.node.pending_state = operation.state;
      target.node.pending_operation_id = operation.client_operation_id;
    }
  }

  const live = new Map<string, EffectiveNode>();
  for (const [nodeId, entry] of table) {
    if (!entry.deleted) live.set(nodeId, entry);
  }
  // 删除 Folder 必须连同逻辑后代一起消失，否则被删子树会在投影里继续可交互。
  let changed = true;
  while (changed) {
    changed = false;
    for (const [nodeId, entry] of [...live]) {
      const parentId = entry.node.parent_node_id;
      if (parentId === null) continue;
      if (table.get(parentId)?.deleted === true && !live.has(parentId)) {
        live.delete(nodeId);
        changed = true;
      }
    }
  }

  for (const entry of live.values()) {
    assertParentChainIsAcyclic(live, entry.node.node_id);
  }
  const childCounts = new Map<string, number>();
  for (const entry of live.values()) {
    const parentId = entry.node.parent_node_id;
    if (parentId === null) continue;
    childCounts.set(parentId, (childCounts.get(parentId) ?? 0) + 1);
  }
  for (const [nodeId, entry] of live) {
    if (entry.confirmed) continue;
    entry.node.has_children = (childCounts.get(nodeId) ?? 0) > 0;
  }
  return live;
}

function assertParentChainIsAcyclic(
  live: ReadonlyMap<string, EffectiveNode>,
  nodeId: string,
): void {
  const seen = new Set<string>();
  let cursor: string | null = nodeId;
  while (cursor !== null) {
    if (seen.has(cursor)) {
      throw new Error(`pending 投影包含祖先环: ${nodeId}`);
    }
    seen.add(cursor);
    cursor = live.get(cursor)?.node.parent_node_id ?? null;
  }
}

/**
 * 投影单个已加载分支：confirmed 子节点保持后端顺序，本地 pending 节点追加在后并单独标注。
 *
 * `cursor`/`total`/`revision` 逐字复制后端 confirmed 页，绝不因 pending 变更调整。
 */
export function projectCatalogBranch(
  page: SessionCatalogPage,
  input: CatalogProjectionInput,
): CatalogBranchProjection {
  const parentNodeId = page.parent_node_id ?? null;
  const live = buildEffectiveNodes(input);
  const confirmedChildren = page.items.map((item) => item.node_id);
  const items: CatalogProjectedNode[] = [];
  const locallyRemoved: string[] = [];
  for (const nodeId of confirmedChildren) {
    const entry = live.get(nodeId);
    // 被本地 pending 移出本分支/删除的 confirmed 子节点必须从本分支消失：否则同一 node
    // 会在源分支与目标分支同时显示。
    if (!entry || entry.node.parent_node_id !== parentNodeId) {
      locallyRemoved.push(nodeId);
      continue;
    }
    items.push(entry.node);
  }
  const known = new Set(confirmedChildren);
  const pendingExtras = [...live.values()]
    .filter((entry) => !known.has(entry.node.node_id)
      && entry.node.parent_node_id === parentNodeId)
    .map((entry) => entry.node)
    .sort((left, right) => (left.pending_operation_id ?? "").localeCompare(
      right.pending_operation_id ?? "",
    ));
  // pending 标注覆盖本分支的**全部**节点：confirmed 节点被本地改名/移动后同样必须以
  // pending 状态展示，不能因为它是后端来的就显示成已提交。
  const pendingNodeIds = [...items, ...pendingExtras]
    .filter((node) => node.pending_state !== null)
    .map((node) => node.node_id);
  return {
    revision: page.revision,
    parent_node_id: parentNodeId,
    items: [...items, ...pendingExtras],
    cursor: page.cursor ?? null,
    total: page.total,
    consistency_warning: page.consistency_warning ?? null,
    pending_node_ids: pendingNodeIds,
    locally_removed_node_ids: locallyRemoved,
  };
}

export interface CatalogBreadcrumbProjection {
  revision: string;
  items: CatalogProjectedNode[];
  pending_node_ids: string[];
}

/**
 * 投影面包屑：pending 新建的 Folder 作为显式本地节点参与父链，pending 移动/改名立即生效。
 *
 * 缺失 node 时定位最近的有效祖先（截断到该祖先为止），绝不返回悬空父链。
 */
export function projectCatalogBreadcrumb(
  confirmed: { revision: string; items: readonly SessionCatalogNode[] },
  input: CatalogProjectionInput,
  nodeId: string,
): CatalogBreadcrumbProjection {
  const live = buildEffectiveNodes(input);
  const chain: CatalogProjectedNode[] = [];
  let cursor: string | null = nodeId;
  while (cursor !== null) {
    const entry = live.get(cursor);
    if (!entry) break;
    chain.unshift(entry.node);
    cursor = entry.node.parent_node_id;
  }
  if (chain.length === 0) {
    return { revision: confirmed.revision, items: [...confirmed.items].map(confirmedNodeToProjected), pending_node_ids: [] };
  }
  return {
    revision: confirmed.revision,
    items: chain,
    pending_node_ids: chain
      .filter((node) => node.pending_state !== null)
      .map((node) => node.node_id),
  };
}

export interface CatalogSearchProjection {
  revision: string;
  items: CatalogProjectedNode[];
  cursor: string | null;
  total: number;
  pending_node_ids: string[];
}

/**
 * 投影搜索结果：confirmed 命中保持后端顺序与 `cursor`/`total`，本地 pending 命中显式追加。
 *
 * pending 项绝不写入后端分页语义，因此不改变 `total` 也不产生新 cursor。
 */
export function projectCatalogSearch(
  confirmed: { revision: string; cursor: string | null; total: number },
  input: CatalogProjectionInput,
  query: string,
): CatalogSearchProjection {
  const normalized = query.trim().toLocaleLowerCase();
  if (normalized === "") {
    throw new Error("会话目录搜索关键字不能为空");
  }
  const live = buildEffectiveNodes(input);
  const pendingMatches = [...live.values()]
    .filter((entry) => entry.node.pending_state !== null
      && entry.node.name.toLocaleLowerCase().includes(normalized))
    .map((entry) => entry.node);
  return {
    revision: confirmed.revision,
    items: pendingMatches,
    cursor: confirmed.cursor,
    total: confirmed.total,
    pending_node_ids: pendingMatches.map((node) => node.node_id),
  };
}

/** 本地 pending 节点在计数展示中的唯一口径：单独报告，不并入后端 total。 */
export function catalogPendingNodeCount(input: CatalogProjectionInput): number {
  const live = buildEffectiveNodes(input);
  return [...live.values()].filter((entry) => entry.node.pending_state !== null).length;
}
