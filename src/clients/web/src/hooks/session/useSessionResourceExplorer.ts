import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getSessionCatalogBreadcrumb,
  listSessionCatalogChildren,
  refreshSessionCatalog,
} from "../../api";
import { fetchSessionCatalogSnapshot } from "../../api/session/sessionCatalogOperations";
import {
  createWorkspaceNavigationFolder,
  deleteWorkspaceNavigationFolder,
  getWorkspaceNavigation,
  placeWorkspaceNavigationNode,
  renameWorkspaceNavigationFolder,
  searchGatewaySessionCatalog,
} from "../../gatewayApi";
import type {
  GatewaySessionSearchResults,
  GatewayUserAccess,
  SessionCatalogNode,
  SessionCatalogPage,
  WorkspaceNavigationTree,
} from "../../types/backend";
import {
  projectCatalogBranch,
  projectCatalogBreadcrumb,
  type CatalogProjectedNode,
} from "../../state/session/sessionCatalogProjection";
import {
  catalogOutboxOperationIsTerminal,
  type CatalogOutbox,
  type CatalogOutboxIntent,
} from "../../state/session/sessionCatalogOutbox";
import {
  sessionCatalogOutboxScopeKey,
  type SessionCatalogOutboxRuntime,
  type SessionCatalogOutboxSnapshot,
} from "./sessionCatalogOutboxRuntime";
import type { SessionGeneratorResourcesController } from "../sessionResourceExplorer/useSessionGeneratorResources";
import { changedCatalogWorkspaceIds } from "../sessionResourceExplorer/resourceTreeSync";
import { errorMessage } from "../../utils/errorMessage";
import { trackInFlightRequest } from "../runtime/inFlightRequests";
import { HttpRequestError, isTransientNetworkError } from "../../api/http";
import { isNavigationBackpressureStatus } from "../../api/session/sessionCatalogOperations";
import {
  requireGatewayWorkspaceRequestTarget,
  type GatewayWorkspaceRequestTarget,
} from "../../api/workspaceIdentity";

export interface CatalogBranchState extends Omit<SessionCatalogPage, "catalog_revision"> {
  /** 请求尚未取得后端 snapshot 时为空；已加载页总是由 DTO 提供数值 generation。 */
  catalog_revision?: number;
  loading: boolean;
  error: string | null;
}

export interface ProjectedCatalogBranchState extends Omit<CatalogBranchState, "items"> {
  items: CatalogProjectedNode[];
  pending_node_ids: string[];
  locally_removed_node_ids: string[];
}

const emptySearch: GatewaySessionSearchResults = {
  items: [],
  workspaces: [],
  total: 0,
};

const CATALOG_RETRY_LIMIT = 3;

/**
 * 目录分支读取是否可重试的唯一判据：只认「明确的错误类型 + 状态码」，不再对错误
 * 文案做正则嗅探——`HttpRequestError` 的消息形如 `请求失败 503 : <detail>`，与
 * 旧正则要求的字面 `HTTP 503` 永不匹配，会让背压重试静默退化为 1 次。
 *
 * - `HttpRequestError` 的背压状态（408/425/429/502/503/504，权威集合见
 *   `NAVIGATION_BACKPRESSURE_STATUSES`）→ 可重试；
 * - fetch 网络层瞬时故障（Gateway/前端热切换窗口内的 TypeError、AbortError
 *   等）→ 复用 `isTransientNetworkError`，不另写一套判据；
 * - 其余错误（4xx 明确拒绝、500 内部故障、超时）一律不重试。
 */
function isRetryableCatalogError(error: unknown): boolean {
  if (error instanceof HttpRequestError) {
    return isNavigationBackpressureStatus(error.status);
  }
  return isTransientNetworkError(error);
}

function catalogRetryDelay(attempt: number): Promise<void> {
  return new Promise((resolve) => {
    // 基础设施计时用 globalThis，与 http.ts、turnLoadSupport 等既有实现一致；
    // 不能依赖 window 全局，否则在无 window 的运行环境里重试退避会直接抛错。
    globalThis.setTimeout(resolve, 250 * (attempt + 1));
  });
}

function branchKey(workspaceId: string, parentNodeId?: string | null): string {
  return `${workspaceId}:${parentNodeId ?? "root"}`;
}

function updateParentNodeChildFlag(
  branches: Map<string, CatalogBranchState>,
  workspaceId: string,
  parentNodeId: string | null | undefined,
  hasChildren: boolean,
): void {
  if (!parentNodeId) {
    return;
  }
  for (const [key, branch] of branches) {
    if (!key.startsWith(`${workspaceId}:`)) {
      continue;
    }
    const parentIndex = branch.items.findIndex((item) => item.node_id === parentNodeId);
    if (parentIndex < 0 || branch.items[parentIndex].has_children === hasChildren) {
      continue;
    }
    const items = [...branch.items];
    items[parentIndex] = { ...items[parentIndex], has_children: hasChildren };
    branches.set(key, { ...branch, items });
  }
}

/**
 * 读取失败的统一终态：沿用上一次的分页快照，只把 loading 落下并带上错误文案。
 * 分支读取与目录刷新失败都必须收敛到这一个状态构造，避免两处各写一份八字段对象。
 */
function branchFailureState(
  branch: CatalogBranchState | undefined,
  parentNodeId: string | null,
  message: string,
): CatalogBranchState {
  return {
    revision: branch?.revision ?? "",
    ...(branch?.catalog_revision === undefined
      ? {}
      : { catalog_revision: branch.catalog_revision }),
    parent_node_id: parentNodeId,
    items: branch?.items ?? [],
    cursor: branch?.cursor ?? null,
    total: branch?.total ?? 0,
    consistency_warning: branch?.consistency_warning ?? null,
    loading: false,
    error: message,
  };
}

function projectConfirmedNode(node: SessionCatalogNode): CatalogProjectedNode {
  return {
    node_id: node.node_id,
    kind: node.kind,
    name: node.name,
    parent_node_id: node.parent_node_id ?? null,
    session_id: node.session_id ?? null,
    folder_id: node.folder_id ?? null,
    has_children: node.has_children,
    node_revision: node.node_revision,
    state: node.state,
    storage_relative_path: node.storage_relative_path ?? null,
    created_at: node.created_at ?? null,
    updated_at: node.updated_at ?? null,
    session: node.session ?? null,
    pending_state: null,
    pending_operation_id: null,
    client_ref: null,
  };
}

export function useSessionResourceExplorer({
  apiPort,
  workspaces,
  activeWorkspaceId,
  searchOpen,
  searchQuery,
  currentSessionId,
  workspaceNavigationSyncKey,
  catalogSyncKeys,
  catalogRefreshVersions,
  generatorResources,
  sessionCatalogOutboxRuntime,
  sessionCatalogOutboxSnapshot,
  gatewayUserAccess,
}: {
  apiPort: number;
  workspaces: readonly GatewayWorkspaceRequestTarget[];
  activeWorkspaceId: string | null;
  searchOpen: boolean;
  searchQuery: string;
  currentSessionId: string;
  workspaceNavigationSyncKey: string;
  catalogSyncKeys: ReadonlyMap<string, string>;
  catalogRefreshVersions: ReadonlyMap<string, number>;
  generatorResources: SessionGeneratorResourcesController;
  sessionCatalogOutboxRuntime?: SessionCatalogOutboxRuntime | null;
  sessionCatalogOutboxSnapshot?: SessionCatalogOutboxSnapshot;
  gatewayUserAccess?: GatewayUserAccess | null;
}) {
  const [navigation, setNavigation] = useState<WorkspaceNavigationTree | null>(null);
  const navigationRef = useRef(navigation);
  navigationRef.current = navigation;
  const [navigationError, setNavigationError] = useState<string | null>(null);
  // 定位当前会话失败与「工作区列表不可用」是两个不同层级的问题：前者只是当前会话
  // 没能自动展开，工作区导航本身仍然可用。若写进 navigationError，界面会误报
  // 「无法加载工作区列表」，并把重试指向重新加载工作区列表，用户点它永远不会重试定位。
  const [revealError, setRevealError] = useState<string | null>(null);
  const [branches, setBranches] = useState<Map<string, CatalogBranchState>>(new Map());
  const branchesRef = useRef(branches);
  branchesRef.current = branches;
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [searchResults, setSearchResults] = useState(emptySearch);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const {
    generators,
    generationRuns,
    generatorError,
    createGenerator,
    refreshGenerationRuns,
    runGenerator,
    updateGenerator,
    deleteGenerator,
    previewGenerator,
  } = generatorResources;
  const currentSessionRevealKeyRef = useRef<string | null>(null);
  const currentSessionRevealRequestRef = useRef(0);
  const navigationRequestRef = useRef(0);
  const branchRequestRefs = useRef<Map<string, number>>(new Map());
  const catalogRefreshInFlightRef = useRef<Map<string, Promise<SessionCatalogPage[]>>>(new Map());
  // 导航请求的发布闸门：只有最新一次请求能把结果写回，且成功发布必须同时清掉
  // 上一次的导航错误。六处调用点只允许走这一个入口，杜绝「漏清错误」或
  // 「迟到响应覆盖新树」在某个分支被单独写错。
  const publishNavigation = useCallback((requestId: number, next: WorkspaceNavigationTree) => {
    if (navigationRequestRef.current !== requestId) {
      return;
    }
    setNavigation(next);
    setNavigationError(null);
  }, []);

  const branchInFlightRequestsRef = useRef<
    Map<string, Promise<SessionCatalogPage | undefined>>
  >(new Map());
  const catalogSyncKeysRef = useRef(catalogSyncKeys);
  const catalogRefreshVersionsRef = useRef(catalogRefreshVersions);
  const generationOutputSyncKeysRef = useRef<ReadonlyMap<string, string>>(new Map());
  const terminalOutboxSyncKeysRef = useRef<ReadonlyMap<string, string>>(new Map());
  const trackedOutboxOperationKeysRef = useRef<Set<string>>(new Set());

  const workspaceRequestTarget = useCallback(
    (workspaceId: string) => requireGatewayWorkspaceRequestTarget(workspaces, workspaceId),
    [workspaces],
  );

  const openCatalogOutbox = useCallback(async (workspaceId: string) => {
    if (!sessionCatalogOutboxRuntime || !gatewayUserAccess) {
      throw new Error("Gateway 认证状态尚未就绪，不能编辑会话目录");
    }
    return await sessionCatalogOutboxRuntime.openWorkspace(
      workspaceRequestTarget(workspaceId),
      gatewayUserAccess,
    );
  }, [gatewayUserAccess, sessionCatalogOutboxRuntime, workspaceRequestTarget]);

  const readCatalogOutbox = useCallback((workspaceId: string) => {
    if (!gatewayUserAccess || !sessionCatalogOutboxSnapshot) return null;
    const key = sessionCatalogOutboxScopeKey(workspaceId, gatewayUserAccess);
    return sessionCatalogOutboxSnapshot.get(key)?.outbox ?? null;
  }, [gatewayUserAccess, sessionCatalogOutboxSnapshot]);

  const catalogRevisionFor = useCallback(async (workspaceId: string): Promise<number> => {
    const prefix = `${workspaceId}:`;
    const revisions = [...branchesRef.current.entries()]
      .filter(([key, branch]) => key.startsWith(prefix)
        && branch.catalog_revision !== undefined)
      .map(([, branch]) => branch.catalog_revision as number);
    if (revisions.length > 0) return Math.max(...revisions);
    const catalog = await fetchSessionCatalogSnapshot(
      apiPort,
      workspaceRequestTarget(workspaceId),
    );
    return catalog.catalog_revision;
  }, [apiPort, workspaceRequestTarget]);

  const confirmedNodeFor = useCallback((workspaceId: string, nodeId: string) => {
    const prefix = `${workspaceId}:`;
    const matches = [...branchesRef.current.entries()]
      .filter(([key]) => key.startsWith(prefix))
      .flatMap(([, branch]) => branch.items)
      .filter((node) => node.node_id === nodeId);
    if (matches.length === 0) {
      throw new Error(`会话目录节点尚未在 confirmed 分支加载: ${workspaceId}/${nodeId}`);
    }
    const node = matches[0];
    if (matches.some((item) => item.node_revision !== node.node_revision)) {
      throw new Error(`会话目录节点 revision 在已加载分支间冲突: ${nodeId}`);
    }
    return node;
  }, []);

  const parentCreationFor = useCallback((outbox: CatalogOutbox, parentNodeId: string | null) => {
    if (!parentNodeId) return null;
    const operation = outbox.operations.find(
      (item) => item.kind === "create_folder"
        && (item.client_ref === parentNodeId || item.created_node_id === parentNodeId),
    );
    if (!operation) return null;
    return catalogOutboxOperationIsTerminal(operation)
      ? { parentNodeId: operation.created_node_id, createdBy: null }
      : { parentNodeId: null, createdBy: operation.client_operation_id };
  }, []);

  const submitCatalogIntent = useCallback(async (
    workspaceId: string,
    intent: CatalogOutboxIntent,
    options: { expectedRevision?: number | null },
  ) => {
    if (!sessionCatalogOutboxRuntime || !gatewayUserAccess) {
      throw new Error("Gateway 认证状态尚未就绪，不能编辑会话目录");
    }
    await openCatalogOutbox(workspaceId);
    const baseCatalogRevision = await catalogRevisionFor(workspaceId);
    const intentOptions = {
      baseCatalogRevision,
      ...(options.expectedRevision === undefined
        ? {}
        : { expectedRevision: options.expectedRevision }),
    };
    const submitted = await sessionCatalogOutboxRuntime.submit(
      workspaceRequestTarget(workspaceId),
      gatewayUserAccess,
      intent,
      intentOptions,
    );
    return submitted;
  }, [catalogRevisionFor, gatewayUserAccess, openCatalogOutbox, sessionCatalogOutboxRuntime, workspaceRequestTarget]);

  const refreshNavigation = useCallback(async () => {
    const requestId = navigationRequestRef.current + 1;
    navigationRequestRef.current = requestId;
    try {
      const next = await getWorkspaceNavigation(apiPort);
      publishNavigation(requestId, next);
      return next;
    } catch (error) {
      const message = errorMessage(error);
      if (navigationRequestRef.current === requestId) {
        setNavigationError(message);
      }
      throw error;
    }
  }, [apiPort, publishNavigation]);

  const loadBranch = useCallback(async (
    workspaceId: string,
    parentNodeId?: string | null,
    append = false,
  ): Promise<SessionCatalogPage | undefined> => {
    if (sessionCatalogOutboxRuntime && gatewayUserAccess) {
      void openCatalogOutbox(workspaceId).catch((error: unknown) => {
        sessionCatalogOutboxRuntime.reportError(
          `恢复会话目录 outbox 失败 (${workspaceId}): ${errorMessage(error)}`,
        );
      });
    }
    const key = branchKey(workspaceId, parentNodeId);
    const requestKey = `${key}:${append ? "append" : "replace"}`;
    const inFlight = branchInFlightRequestsRef.current.get(requestKey);
    if (inFlight) {
      return inFlight;
    }
    const requestId = (branchRequestRefs.current.get(key) ?? 0) + 1;
    branchRequestRefs.current.set(key, requestId);
    const current = branchesRef.current.get(key);
    setBranches((previous) => {
      const next = new Map(previous);
      next.set(key, {
        revision: current?.revision ?? "",
        ...(current?.catalog_revision === undefined
          ? {}
          : { catalog_revision: current.catalog_revision }),
        parent_node_id: parentNodeId ?? null,
        items: current?.items ?? [],
        cursor: current?.cursor ?? null,
        total: current?.total ?? 0,
        consistency_warning: current?.consistency_warning ?? null,
        loading: true,
        error: null,
      });
      return next;
    });
    const request = (async () => {
      try {
        let page: SessionCatalogPage;
        for (let attempt = 0; ; attempt += 1) {
          try {
            page = await listSessionCatalogChildren(
              apiPort,
              workspaceRequestTarget(workspaceId),
              parentNodeId,
              append ? current?.cursor : null,
            );
            break;
          } catch (error) {
            if (attempt >= CATALOG_RETRY_LIMIT - 1 || !isRetryableCatalogError(error)) {
              throw error;
            }
            await catalogRetryDelay(attempt);
          }
        }
        if (branchRequestRefs.current.get(key) !== requestId) {
          return page;
        }
        setBranches((previous) => {
          const next = new Map(previous);
          const previousItems = append ? next.get(key)?.items ?? [] : [];
          const items = [...previousItems, ...page.items];
          next.set(key, {
            ...page,
            items,
            loading: false,
            error: null,
          });
          updateParentNodeChildFlag(
            next,
            workspaceId,
            parentNodeId,
            page.total > 0,
          );
          return next;
        });
        return page;
      } catch (error) {
        if (branchRequestRefs.current.get(key) !== requestId) {
          return undefined;
        }
        const message = errorMessage(error);
        setBranches((previous) => {
          const next = new Map(previous);
          next.set(key, branchFailureState(next.get(key), parentNodeId ?? null, message));
          return next;
        });
        throw error;
      }
    })();
    trackInFlightRequest(branchInFlightRequestsRef.current, requestKey, request);
    return request;
  }, [apiPort, gatewayUserAccess, openCatalogOutbox, sessionCatalogOutboxRuntime, workspaceRequestTarget]);

  const loadCatalogBranch = useCallback(async (
    workspaceId: string,
    parentNodeId: string | null,
  ): Promise<SessionCatalogPage> => {
    const key = branchKey(workspaceId, parentNodeId);
    const request = loadBranch(workspaceId, parentNodeId);
    const requestId = branchRequestRefs.current.get(key);
    const page = await request;
    if (!page || branchRequestRefs.current.get(key) !== requestId) {
      throw new Error(
        `会话目录分支刷新已被更新请求取代: ${workspaceId}/${parentNodeId ?? "root"}`,
      );
    }
    return page;
  }, [loadBranch]);

  const pruneOutboxAfterBranchRefresh = useCallback(async (
    workspaceId: string,
    pages: readonly SessionCatalogPage[],
    operationIds?: readonly string[],
  ) => {
    if (!sessionCatalogOutboxRuntime || !gatewayUserAccess
      || pages.length === 0 || operationIds?.length === 0) return;
    const handle = await openCatalogOutbox(workspaceId);
    const operations = handle.driver.current().operations;
    const selectedIds = new Set(operationIds ?? operations.map(
      (operation) => operation.client_operation_id,
    ));
    const minimumRevision = Math.min(...pages.map((page) => {
      if (!Number.isSafeInteger(page.catalog_revision) || page.catalog_revision < 0) {
        throw new Error(`目录分支缺少有效 catalog revision: ${handle.gatewayWorkspaceId}`);
      }
      return page.catalog_revision;
    }));
    const maximumRevision = Math.max(...pages.map((page) => page.catalog_revision));
    const pruneableOperationIds = operations
      .filter((operation) => selectedIds.has(operation.client_operation_id)
        && catalogOutboxOperationIsTerminal(operation)
        && (operation.committed_catalog_revision === null
          || operation.committed_catalog_revision <= minimumRevision))
      .map((operation) => operation.client_operation_id);
    if (pruneableOperationIds.length > 0) {
      await handle.driver.prune(maximumRevision, pruneableOperationIds);
    }
  }, [gatewayUserAccess, openCatalogOutbox, sessionCatalogOutboxRuntime]);

  const reconcileCatalogBranches = useCallback(async (
    workspaceId: string,
    parentNodeIds: Array<string | null>,
    operationIds?: readonly string[],
  ) => {
    const uniqueParentNodeIds = [...new Set(parentNodeIds)];
    const results = await Promise.all(uniqueParentNodeIds.map(async (parentNodeId) => {
      try {
        return { page: await loadCatalogBranch(workspaceId, parentNodeId), error: null };
      } catch (error) {
        return {
          page: null,
          error: `${parentNodeId ?? "root"}: ${errorMessage(error)}`,
        };
      }
    }));
    const errors = results.flatMap((result) => result.error === null ? [] : [result.error]);
    if (errors.length > 0) {
      throw new Error(`重新读取会话目录失败: ${errors.join("；")}`);
    }
    const pages = results.flatMap((result) => result.page === null ? [] : [result.page]);
    await pruneOutboxAfterBranchRefresh(workspaceId, pages, operationIds);
    return pages;
  }, [loadCatalogBranch, pruneOutboxAfterBranchRefresh]);

  const refreshCatalogWorkspace = useCallback(async (
    workspaceId: string,
    options: {
      additionalParentNodeIds?: readonly (string | null)[];
      operationIds?: readonly string[];
      forceRoot?: boolean;
    } = {},
  ) => {
    const parentNodeIds = new Set<string | null>();
    for (const [key, branch] of branchesRef.current.entries()) {
      if (key.startsWith(`${workspaceId}:`)) {
        parentNodeIds.add(branch.parent_node_id ?? null);
      }
    }
    for (const parentNodeId of options.additionalParentNodeIds ?? []) {
      parentNodeIds.add(parentNodeId);
    }
    const scopedSnapshotKey = gatewayUserAccess
      ? sessionCatalogOutboxScopeKey(workspaceId, gatewayUserAccess)
      : null;
    const currentOutbox = scopedSnapshotKey
      ? sessionCatalogOutboxRuntime?.getSnapshot().get(scopedSnapshotKey)?.outbox
      : null;
    const terminalOperations = currentOutbox?.operations.filter(
      catalogOutboxOperationIsTerminal,
    ) ?? [];
    const operationIds = options.operationIds
      ?? terminalOperations.map((operation) => operation.client_operation_id);
    for (const operation of terminalOperations) {
      if (operation.kind === "create_folder") {
        parentNodeIds.add(operation.parent_node_id);
      }
      if (operation.target_node_id) {
        for (const [key, branch] of branchesRef.current.entries()) {
          if (!key.startsWith(`${workspaceId}:`)) continue;
          const target = branch.items.find((node) => node.node_id === operation.target_node_id);
          if (target) parentNodeIds.add(target.parent_node_id ?? null);
        }
      }
      if (operation.kind === "move_node") {
        parentNodeIds.add(operation.parent_node_id);
      }
    }
    const hasTerminalOutboxOperation = terminalOperations.length > 0;
    if (options.forceRoot || hasTerminalOutboxOperation
      || operationIds.length > 0
      || workspaceId === activeWorkspaceId || parentNodeIds.size > 0) {
      parentNodeIds.add(null);
    }
    const refreshKey = `${workspaceId}\u0000${options.forceRoot ? "root" : ""}\u0000`
      + [...operationIds].sort().join("\u0001");
    const inFlight = catalogRefreshInFlightRef.current.get(refreshKey);
    if (inFlight) return await inFlight;
    const request = reconcileCatalogBranches(workspaceId, [...parentNodeIds], operationIds);
    catalogRefreshInFlightRef.current.set(refreshKey, request);
    try {
      return await request;
    } finally {
      if (catalogRefreshInFlightRef.current.get(refreshKey) === request) {
        catalogRefreshInFlightRef.current.delete(refreshKey);
      }
    }
  }, [
    activeWorkspaceId,
    gatewayUserAccess,
    reconcileCatalogBranches,
    sessionCatalogOutboxRuntime,
  ]);

  const refreshResourceTree = useCallback(async () => {
    if (activeWorkspaceId && sessionCatalogOutboxRuntime && gatewayUserAccess) {
      void openCatalogOutbox(activeWorkspaceId).catch((error: unknown) => {
        sessionCatalogOutboxRuntime.reportError(
          `恢复会话目录 outbox 失败 (${activeWorkspaceId}): ${errorMessage(error)}`,
        );
      });
    }
    const navigationPromise = refreshNavigation();
    const catalogPromise = activeWorkspaceId
      ? (async () => {
          const rootKey = branchKey(activeWorkspaceId, null);
          const requestId = (branchRequestRefs.current.get(rootKey) ?? 0) + 1;
          branchRequestRefs.current.set(rootKey, requestId);
          let rootPage: SessionCatalogPage;
          try {
            rootPage = await refreshSessionCatalog(
              apiPort,
              workspaceRequestTarget(activeWorkspaceId),
            );
          } catch (error) {
            // 上面已经顶掉在途的根分支读取，它的结果不会再写回；刷新自身失败时
            // 必须由这里写入失败终态，否则根分支会永远停在 loadBranch 留下的
            // loading 占位上，界面永久显示「正在加载…」。
            if (branchRequestRefs.current.get(rootKey) === requestId) {
              const message = errorMessage(error);
              setBranches((previous) => {
                const next = new Map(previous);
                next.set(rootKey, branchFailureState(next.get(rootKey), null, message));
                return next;
              });
            }
            throw error;
          }
          if (branchRequestRefs.current.get(rootKey) === requestId) {
            setBranches((previous) => {
              const next = new Map(previous);
              next.set(rootKey, {
                ...rootPage,
                loading: false,
                error: null,
              });
              return next;
            });
          }
          const nestedParentIds = [...branchesRef.current.entries()]
            .filter(([key, branch]) =>
              key.startsWith(`${activeWorkspaceId}:`)
              && branch.parent_node_id,
            )
            .map(([_key, branch]) => branch.parent_node_id as string);
          await Promise.all(
            nestedParentIds.map((parentNodeId) =>
              loadBranch(activeWorkspaceId, parentNodeId),
            ),
          );
        })()
      : Promise.resolve();
    const [nextNavigation] = await Promise.all([
      navigationPromise,
      catalogPromise,
    ]);
    return nextNavigation;
  }, [
    activeWorkspaceId,
    apiPort,
    gatewayUserAccess,
    loadBranch,
    openCatalogOutbox,
    refreshNavigation,
    sessionCatalogOutboxRuntime,
    workspaceRequestTarget,
  ]);

  const loadBranchUntilNode = useCallback(async (
    workspaceId: string,
    parentNodeId: string | null,
    targetNodeId: string,
  ) => {
    const key = branchKey(workspaceId, parentNodeId);
    const pendingRequest = [...branchInFlightRequestsRef.current.entries()]
      .find(([requestKey]) => requestKey.startsWith(`${key}:`))?.[1];
    if (pendingRequest) {
      await pendingRequest;
    }
    const loaded = branchesRef.current.get(key);
    if (loaded?.items.some((item) => item.node_id === targetNodeId)) {
      return;
    }
    let refreshedPage: SessionCatalogPage | undefined;
    if (loaded && !loaded.cursor) {
      // “已加载且没有 cursor”只代表某个时间点的完整第一页，不代表目标节点
      // 永远不存在。创建会话、目录移动等操作可能刚刚更新了权威索引，此时
      // 必须重新读取该分支，再决定目标确实不存在并报告错误。
      refreshedPage = await loadBranch(workspaceId, parentNodeId);
      if (refreshedPage?.items.some((item) => item.node_id === targetNodeId)) {
        return;
      }
    }
    const effectiveLoaded = refreshedPage
      ? {
          ...refreshedPage,
          loading: false,
          error: null,
        }
      : loaded;
    if (effectiveLoaded && !effectiveLoaded.cursor) {
      throw new Error(
        `目录分支已加载但未找到定位节点: workspace=${workspaceId}, node=${targetNodeId}`,
      );
    }
    const requestId = (branchRequestRefs.current.get(key) ?? 0) + 1;
    branchRequestRefs.current.set(key, requestId);
    setBranches((previous) => {
      const next = new Map(previous);
      next.set(key, {
        revision: effectiveLoaded?.revision ?? "",
        ...(effectiveLoaded?.catalog_revision === undefined
          ? {}
          : { catalog_revision: effectiveLoaded.catalog_revision }),
        parent_node_id: parentNodeId,
        items: effectiveLoaded?.items ?? [],
        cursor: effectiveLoaded?.cursor ?? null,
        total: effectiveLoaded?.total ?? 0,
        loading: true,
        error: null,
      });
      return next;
    });
    const items: SessionCatalogPage["items"] = [
      ...(effectiveLoaded?.items ?? []),
    ];
    const visitedCursors = new Set<string>();
    let cursor: string | null = effectiveLoaded?.cursor ?? null;
    try {
      while (true) {
        const page = await listSessionCatalogChildren(
          apiPort,
          workspaceRequestTarget(workspaceId),
          parentNodeId,
          cursor,
        );
        if (branchRequestRefs.current.get(key) !== requestId) {
          return;
        }
        items.push(...page.items);
        const found = items.some((item) => item.node_id === targetNodeId);
        setBranches((previous) => {
          const next = new Map(previous);
          next.set(key, {
            ...page,
            items: [...items],
            loading: !found && page.cursor !== null,
            error: null,
          });
          return next;
        });
        if (found) {
          return;
        }
        if (!page.cursor) {
          throw new Error(
            `目录分页结束但未找到定位节点: workspace=${workspaceId}, node=${targetNodeId}`,
          );
        }
        if (visitedCursors.has(page.cursor)) {
          throw new Error(
            `目录分页 cursor 循环: workspace=${workspaceId}, cursor=${page.cursor}`,
          );
        }
        visitedCursors.add(page.cursor);
        cursor = page.cursor;
      }
    } catch (error) {
      // 请求身份守卫：本次分页已被更新的一次 loadBranch（或刷新）顶掉时，它的
      // 失败终态不得覆盖那份已成功写入的新结果。与 loadBranch 成功/失败分支
      // （同文件使用 `branchRequestRefs.current.get(key) !== requestId` 同一手
      // 法）保持一致，不再新造第二套 generation 机制。
      if (branchRequestRefs.current.get(key) !== requestId) {
        throw error;
      }
      const message = errorMessage(error);
      setBranches((previous) => {
        const next = new Map(previous);
        const branch = next.get(key);
        next.set(key, {
          revision: branch?.revision ?? "",
          ...(branch?.catalog_revision === undefined
            ? {}
            : { catalog_revision: branch.catalog_revision }),
          parent_node_id: parentNodeId,
          items: branch?.items ?? items,
          cursor: branch?.cursor ?? null,
          total: branch?.total ?? items.length,
          loading: false,
          error: message,
        });
        return next;
      });
      throw error;
    }
  }, [apiPort, loadBranch, workspaceRequestTarget]);

  const toggleExpanded = useCallback((
    id: string,
    workspaceId?: string,
    parentNodeId?: string | null,
  ) => {
    const isExpanded = expandedIds.has(id);
    setExpandedIds((previous) => {
      const next = new Set(previous);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
    if (!isExpanded && workspaceId) {
      const key = branchKey(workspaceId, parentNodeId);
      if (!branchesRef.current.has(key)) {
        void loadBranch(workspaceId, parentNodeId).catch(() => {
          // loadBranch 已把错误保存在对应工作区分支中，由该分支提供恢复入口。
        });
      }
    }
  }, [expandedIds, loadBranch]);

  const createWorkspaceFolder = useCallback(async (
    name: string,
    parentNodeId?: string | null,
  ) => {
    const requestId = navigationRequestRef.current + 1;
    navigationRequestRef.current = requestId;
    const next = await createWorkspaceNavigationFolder(apiPort, name, parentNodeId);
    publishNavigation(requestId, next);
  }, [apiPort, publishNavigation]);

  const revealSearchResult = useCallback(async (
    workspaceId: string,
    breadcrumbNodeIds: string[],
    targetKind: "folder" | "session",
  ) => {
    const idsToExpand = new Set<string>([`workspace:${workspaceId}`]);
    const currentNavigation = navigationRef.current;
    const workspaceRef = currentNavigation?.nodes.find(
      (node) => node.kind === "workspace_ref" && node.workspace_id === workspaceId,
    );
    let navigationParentId = workspaceRef?.parent_node_id ?? null;
    while (navigationParentId) {
      idsToExpand.add(`navigation:${navigationParentId}`);
      navigationParentId = currentNavigation?.nodes.find(
        (node) => node.node_id === navigationParentId,
      )?.parent_node_id ?? null;
    }
    let parentNodeId: string | null = null;
    for (const [index, nodeId] of breadcrumbNodeIds.entries()) {
      await loadBranchUntilNode(workspaceId, parentNodeId, nodeId);
      const isTarget = index === breadcrumbNodeIds.length - 1;
      if (!isTarget || targetKind === "folder") {
        idsToExpand.add(`catalog:${workspaceId}:${nodeId}`);
      }
      parentNodeId = nodeId;
    }
    setExpandedIds((previous) => new Set([...previous, ...idsToExpand]));
    if (targetKind === "folder" && breadcrumbNodeIds.length > 0) {
      await loadBranch(
        workspaceId,
        breadcrumbNodeIds[breadcrumbNodeIds.length - 1],
      );
    }
  }, [loadBranch, loadBranchUntilNode]);

  const revealWorkspaceFolder = useCallback((breadcrumbNodeIds: string[]) => {
    setExpandedIds((previous) => {
      const next = new Set(previous);
      for (const nodeId of breadcrumbNodeIds) {
        next.add(`navigation:${nodeId}`);
      }
      return next;
    });
  }, []);

  const trackCatalogIntent = useCallback((
    workspaceId: string,
    handle: Awaited<ReturnType<SessionCatalogOutboxRuntime["openWorkspace"]>>,
    operationId: string,
    parentNodeIds: Array<string | null>,
  ) => {
    if (!sessionCatalogOutboxRuntime) return;
    const trackedKey = `${handle.partitionKey}\u0000${operationId}`;
    trackedOutboxOperationKeysRef.current.add(trackedKey);
    void (async () => {
      let terminalError: unknown = null;
      try {
        await sessionCatalogOutboxRuntime.waitForTerminal(handle, operationId);
      } catch (error: unknown) {
        terminalError = error;
      }
      const currentOperation = handle.driver.current().operations.find(
        (operation) => operation.client_operation_id === operationId,
      );
      const committed = currentOperation?.state === "committed";
      let refreshError: unknown = null;
      try {
        await refreshCatalogWorkspace(workspaceId, {
          additionalParentNodeIds: parentNodeIds,
          operationIds: [operationId],
        });
      } catch (error: unknown) {
        refreshError = error;
      }
      if (terminalError || refreshError) {
        const status = committed
          ? refreshError
            ? "已提交但目录分支刷新失败"
            : "已提交但终态确认过程失败"
          : refreshError
            ? "未提交或终态未确认，且目录分支刷新失败"
            : "未提交或终态未确认";
        const details = [terminalError, refreshError]
          .filter((error): error is unknown => error !== null)
          .map(errorMessage)
          .join("；");
        sessionCatalogOutboxRuntime.reportError(
          `会话目录操作 ${operationId}${status}: ${details}`,
        );
      }
      trackedOutboxOperationKeysRef.current.delete(trackedKey);
    })();
  }, [refreshCatalogWorkspace, sessionCatalogOutboxRuntime]);

  const createSessionFolder = useCallback(async (
    workspaceId: string,
    name: string,
    parentFolderId?: string | null,
  ) => {
    const handle = await openCatalogOutbox(workspaceId);
    const parent = parentCreationFor(
      handle.driver.current(),
      parentFolderId ?? null,
    );
    if (parent?.createdBy) {
      throw new Error("父文件夹尚未提交，当前目录协议不能在待提交文件夹下新建子文件夹");
    }
    const result = await submitCatalogIntent(workspaceId, {
      kind: "create_folder",
      name,
      parentNodeId: parent ? parent.parentNodeId : parentFolderId ?? null,
    }, {});
    if ("handle" in result) {
      trackCatalogIntent(workspaceId, result.handle, result.operationId, [parentFolderId ?? null]);
    }
    await loadBranch(workspaceId, parentFolderId);
  }, [loadBranch, openCatalogOutbox, parentCreationFor, submitCatalogIntent, trackCatalogIntent]);

  const renameWorkspaceFolder = useCallback(async (nodeId: string, name: string) => {
    const requestId = navigationRequestRef.current + 1;
    navigationRequestRef.current = requestId;
    const next = await renameWorkspaceNavigationFolder(apiPort, nodeId, name);
    publishNavigation(requestId, next);
  }, [apiPort, publishNavigation]);

  const placeWorkspaceNode = useCallback(async (
    nodeId: string,
    parentNodeId: string | null,
    mode: "before" | "after" | "last",
    targetNodeId?: string,
  ) => {
    const requestId = navigationRequestRef.current + 1;
    navigationRequestRef.current = requestId;
    try {
      const next = await placeWorkspaceNavigationNode(apiPort, {
        node_id: nodeId,
        parent_node_id: parentNodeId,
        mode,
        ...(targetNodeId ? { target_node_id: targetNodeId } : {}),
      });
      publishNavigation(requestId, next);
    } catch (operationError) {
      try {
        await refreshNavigation();
      } catch (reconciliationError) {
        throw new Error(
          `${errorMessage(operationError)}；重新读取工作区导航也失败: ${errorMessage(reconciliationError)}`,
        );
      }
      throw operationError;
    }
  }, [apiPort, publishNavigation, refreshNavigation]);

  const deleteWorkspaceFolder = useCallback(async (nodeId: string) => {
    const requestId = navigationRequestRef.current + 1;
    navigationRequestRef.current = requestId;
    const next = await deleteWorkspaceNavigationFolder(apiPort, nodeId);
    publishNavigation(requestId, next);
  }, [apiPort, publishNavigation]);

  const renameSessionFolder = useCallback(async (
    workspaceId: string,
    folderId: string,
    name: string,
    parentFolderId?: string | null,
  ) => {
    const node = confirmedNodeFor(workspaceId, folderId);
    const result = await submitCatalogIntent(workspaceId, {
      kind: "rename_node",
      targetNodeId: folderId,
      name,
    }, { expectedRevision: node.node_revision });
    if ("handle" in result) {
      trackCatalogIntent(workspaceId, result.handle, result.operationId, [parentFolderId ?? null]);
    }
  }, [confirmedNodeFor, submitCatalogIntent, trackCatalogIntent]);

  const moveSessionFolder = useCallback(async (
    workspaceId: string,
    folderId: string,
    parentFolderId?: string | null,
    previousParentId?: string | null,
  ) => {
    const node = confirmedNodeFor(workspaceId, folderId);
    const handle = await openCatalogOutbox(workspaceId);
    const parent = parentCreationFor(handle.driver.current(), parentFolderId ?? null);
    const result = await submitCatalogIntent(workspaceId, {
      kind: "move_node",
      targetNodeId: folderId,
      parentNodeId: parent ? parent.parentNodeId : parentFolderId ?? null,
      parentCreatedByOperationId: parent?.createdBy,
    }, { expectedRevision: node.node_revision });
    if ("handle" in result) {
      trackCatalogIntent(
        workspaceId,
        result.handle,
        result.operationId,
        [previousParentId ?? null, parentFolderId ?? null],
      );
    }
  }, [confirmedNodeFor, openCatalogOutbox, parentCreationFor, submitCatalogIntent, trackCatalogIntent]);

  const deleteSessionFolder = useCallback(async (
    workspaceId: string,
    folderId: string,
    parentFolderId?: string | null,
  ) => {
    let deletedCurrentSession = false;
    const outboxRuntime = sessionCatalogOutboxRuntime;
    if (!outboxRuntime) {
      throw new Error("Gateway 会话目录 outbox runtime 尚未初始化");
    }
    const handle = await openCatalogOutbox(workspaceId);
    const node = confirmedNodeFor(workspaceId, folderId);
    if (workspaceId === activeWorkspaceId && currentSessionId) {
      const breadcrumb = await getSessionCatalogBreadcrumb(
        apiPort,
        workspaceRequestTarget(workspaceId),
        currentSessionId,
      );
      const outbox = handle.driver.current();
      const projected = outbox
        ? projectCatalogBreadcrumb(breadcrumb, { confirmedNodes: breadcrumb.items, outbox }, currentSessionId)
        : breadcrumb;
      deletedCurrentSession = projected.items.some(
        (item) => item.node_id === folderId,
      );
    }
    const result = await submitCatalogIntent(workspaceId, {
      kind: "delete_folder",
      targetNodeId: folderId,
      recursive: true,
    }, { expectedRevision: node.node_revision });
    const trackedKey = `${handle.partitionKey}\u0000${result.operationId}`;
    trackedOutboxOperationKeysRef.current.add(trackedKey);
    try {
      let terminalError: unknown = null;
      try {
        await outboxRuntime.waitForTerminal(handle, result.operationId);
      } catch (error: unknown) {
        terminalError = error;
      }
      const currentOperation = handle.driver.current().operations.find(
        (operation) => operation.client_operation_id === result.operationId,
      );
      const committed = currentOperation?.state === "committed";
      let refreshError: unknown = null;
      try {
        await refreshCatalogWorkspace(workspaceId, {
          additionalParentNodeIds: [parentFolderId ?? null],
          operationIds: [result.operationId],
        });
      } catch (error: unknown) {
        refreshError = error;
      }
      if (terminalError || refreshError) {
        const status = committed
          ? refreshError
            ? "已提交但目录分支刷新失败"
            : "已提交但终态确认过程失败"
          : refreshError
            ? "未提交或终态未确认，且目录分支刷新失败"
            : "未提交或终态未确认";
        const details = [terminalError, refreshError]
          .filter((error): error is unknown => error !== null)
          .map(errorMessage)
          .join("；");
        outboxRuntime.reportError(
          `会话目录操作 ${result.operationId}${status}: ${details}`,
        );
        if (terminalError && refreshError) {
          throw new Error(`${errorMessage(terminalError)}；${errorMessage(refreshError)}`);
        }
        throw terminalError ?? refreshError;
      }
    } finally {
      trackedOutboxOperationKeysRef.current.delete(trackedKey);
    }
    return deletedCurrentSession;
  }, [
    activeWorkspaceId,
    apiPort,
    confirmedNodeFor,
    currentSessionId,
    openCatalogOutbox,
    refreshCatalogWorkspace,
    sessionCatalogOutboxRuntime,
    submitCatalogIntent,
    workspaceRequestTarget,
  ]);

  const moveCatalogNode = useCallback(async (
    workspaceId: string,
    nodeId: string,
    parentNodeId: string | null,
    previousParentNodeId: string | null,
  ) => {
    const node = confirmedNodeFor(workspaceId, nodeId);
    const handle = await openCatalogOutbox(workspaceId);
    const parent = parentCreationFor(handle.driver.current(), parentNodeId);
    const result = await submitCatalogIntent(workspaceId, {
      kind: "move_node",
      targetNodeId: nodeId,
      parentNodeId: parent ? parent.parentNodeId : parentNodeId,
      parentCreatedByOperationId: parent?.createdBy,
    }, { expectedRevision: node.node_revision });
    if ("handle" in result) {
      trackCatalogIntent(
        workspaceId,
        result.handle,
        result.operationId,
        [previousParentNodeId, parentNodeId],
      );
    }
  }, [confirmedNodeFor, openCatalogOutbox, parentCreationFor, submitCatalogIntent, trackCatalogIntent]);

  const assignSessionFolder = useCallback(async (
    workspaceId: string,
    sessionId: string,
    folderId?: string | null,
    previousParentId?: string | null,
  ) => {
    const node = confirmedNodeFor(workspaceId, sessionId);
    const handle = await openCatalogOutbox(workspaceId);
    const parent = parentCreationFor(handle.driver.current(), folderId ?? null);
    const result = await submitCatalogIntent(workspaceId, {
      kind: "move_node",
      targetNodeId: node.node_id,
      parentNodeId: parent ? parent.parentNodeId : folderId ?? null,
      parentCreatedByOperationId: parent?.createdBy,
    }, { expectedRevision: node.node_revision });
    if ("handle" in result) {
      trackCatalogIntent(
        workspaceId,
        result.handle,
        result.operationId,
        [previousParentId ?? null, folderId ?? null],
      );
    }
  }, [confirmedNodeFor, openCatalogOutbox, parentCreationFor, submitCatalogIntent, trackCatalogIntent]);

  const projectedBranches = useMemo(() => {
    const confirmedByWorkspace = new Map<string, Map<string, SessionCatalogNode>>();
    for (const [key, branch] of branches) {
      const workspaceId = key.slice(0, key.indexOf(":"));
      const nodes = confirmedByWorkspace.get(workspaceId) ?? new Map<string, SessionCatalogNode>();
      for (const node of branch.items) {
        const previous = nodes.get(node.node_id);
        if (previous && (previous.node_revision !== node.node_revision
          || previous.parent_node_id !== node.parent_node_id)) {
          throw new Error(`已加载目录分页包含互相冲突的 node revision: ${workspaceId}/${node.node_id}`);
        }
        nodes.set(node.node_id, node);
      }
      confirmedByWorkspace.set(workspaceId, nodes);
    }
    const projected = new Map<string, ProjectedCatalogBranchState>();
    for (const [key, branch] of branches) {
      const workspaceId = key.slice(0, key.indexOf(":"));
      const outbox = readCatalogOutbox(workspaceId);
      const confirmedNodes = [...(confirmedByWorkspace.get(workspaceId)?.values() ?? [])];
      if (outbox && branch.catalog_revision !== undefined) {
        const page: SessionCatalogPage = {
          revision: branch.revision,
          catalog_revision: branch.catalog_revision,
          parent_node_id: branch.parent_node_id,
          items: branch.items,
          cursor: branch.cursor,
          total: branch.total,
          consistency_warning: branch.consistency_warning,
        };
        const projection = projectCatalogBranch(page, { confirmedNodes, outbox });
        projected.set(key, {
          ...branch,
          ...projection,
          loading: branch.loading,
          error: branch.error,
        });
        continue;
      }
      projected.set(key, {
        ...branch,
        items: branch.items.map(projectConfirmedNode),
        pending_node_ids: [],
        locally_removed_node_ids: [],
      });
    }
    return projected;
  }, [branches, readCatalogOutbox]);

  useEffect(() => {
    void refreshNavigation().catch(() => {
      // refreshNavigation 已将错误写入可见的导航错误状态。
    });
  }, [refreshNavigation, workspaceNavigationSyncKey]);

  useEffect(() => {
    const previousSyncKeys = catalogSyncKeysRef.current;
    const previousRefreshVersions = catalogRefreshVersionsRef.current;
    catalogSyncKeysRef.current = catalogSyncKeys;
    catalogRefreshVersionsRef.current = catalogRefreshVersions;
    const changedWorkspaceIds = changedCatalogWorkspaceIds(
      previousSyncKeys,
      catalogSyncKeys,
      previousRefreshVersions,
      catalogRefreshVersions,
    ).filter((workspaceId) =>
      catalogSyncKeys.has(workspaceId)
      || previousRefreshVersions.get(workspaceId)
        !== catalogRefreshVersions.get(workspaceId),
    );
    if (changedWorkspaceIds.length === 0) {
      return;
    }
    void Promise.all(
      changedWorkspaceIds.map((workspaceId) => refreshCatalogWorkspace(workspaceId)),
    ).catch(() => {
      // 每个失败分支都保留自己的可见错误，不能提升为整个资源树故障。
    });
  }, [catalogRefreshVersions, catalogSyncKeys, refreshCatalogWorkspace]);

  useEffect(() => {
    const nextSyncKeys = new Map<string, string>();
    if (sessionCatalogOutboxSnapshot && gatewayUserAccess) {
      for (const [scopeKey, entry] of sessionCatalogOutboxSnapshot) {
        if (scopeKey !== sessionCatalogOutboxScopeKey(
          entry.gatewayWorkspaceId,
          gatewayUserAccess,
        )) {
          continue;
        }
        const terminalOperations = entry.outbox.operations.filter((operation) =>
          catalogOutboxOperationIsTerminal(operation)
          && !trackedOutboxOperationKeysRef.current.has(
            `${entry.partitionKey}\u0000${operation.client_operation_id}`,
          ),
        );
        if (terminalOperations.length === 0) continue;
        const operationIds = terminalOperations.map(
          (operation) => operation.client_operation_id,
        ).sort();
        const signature = terminalOperations.map((operation) =>
          `${operation.client_operation_id}:${operation.state}:${operation.committed_catalog_revision ?? "null"}`,
        ).sort().join("\u0001");
        nextSyncKeys.set(scopeKey, signature);
        if (terminalOutboxSyncKeysRef.current.get(scopeKey) === signature) continue;
        void refreshCatalogWorkspace(entry.gatewayWorkspaceId, { operationIds })
          .then(() => {
            const failures = terminalOperations.filter(
              (operation) => operation.state !== "committed",
            );
            for (const operation of failures) {
              sessionCatalogOutboxRuntime?.reportError(
                `会话目录操作 ${operation.client_operation_id}未提交 (${operation.state})`
                + `: ${operation.error_detail ?? operation.error_code ?? "后端未提供错误详情"}`,
              );
            }
          })
          .catch((error: unknown) => {
            sessionCatalogOutboxRuntime?.reportError(
              `会话目录终态分支刷新失败 (${entry.gatewayWorkspaceId}, `
              + `${operationIds.join(", ")}): ${errorMessage(error)}`,
            );
          });
      }
    }
    terminalOutboxSyncKeysRef.current = nextSyncKeys;
  }, [
    gatewayUserAccess,
    refreshCatalogWorkspace,
    sessionCatalogOutboxRuntime,
    sessionCatalogOutboxSnapshot,
  ]);

  useEffect(() => {
    const nextOutputSyncKeys = new Map<string, string[]>();
    for (const runs of generationRuns.values()) {
      for (const run of runs) {
        for (const output of run.outputs) {
          const keys = nextOutputSyncKeys.get(output.workspace_id) ?? [];
          keys.push(`${run.run_id}\u0000${run.status}\u0000${output.session_id}`);
          nextOutputSyncKeys.set(output.workspace_id, keys);
        }
      }
    }
    const normalizedOutputSyncKeys = new Map(
      [...nextOutputSyncKeys.entries()].map(([workspaceId, keys]) => [
        workspaceId,
        keys.sort().join("\u0001"),
      ]),
    );
    const previousOutputSyncKeys = generationOutputSyncKeysRef.current;
    generationOutputSyncKeysRef.current = normalizedOutputSyncKeys;
    const changedWorkspaceIds = [...normalizedOutputSyncKeys.keys()].filter(
      (workspaceId) =>
        previousOutputSyncKeys.get(workspaceId)
        !== normalizedOutputSyncKeys.get(workspaceId),
    );
    if (changedWorkspaceIds.length === 0) {
      return;
    }
    void Promise.all(changedWorkspaceIds.map((workspaceId) => refreshCatalogWorkspace(workspaceId)))
      .catch(() => {
        // 每个失败分支都保留自己的可见错误，不能提升为整个资源树故障。
      });
  }, [generationRuns, refreshCatalogWorkspace]);

  useEffect(() => {
    if (!activeWorkspaceId) {
      return;
    }
    const id = `workspace:${activeWorkspaceId}`;
    setExpandedIds((previous) => new Set(previous).add(id));
    const key = branchKey(activeWorkspaceId, null);
    if (!branchesRef.current.has(key)) {
      void loadBranch(activeWorkspaceId, null)
        .catch(() => {
          // loadBranch 已把错误保存在当前工作区分支中。
        });
    }
  }, [activeWorkspaceId, loadBranch]);

  useEffect(() => {
    const normalized = searchQuery.trim();
    if (!searchOpen || !normalized) {
      setSearchResults(emptySearch);
      setSearchError(null);
      setSearching(false);
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setSearching(true);
      setSearchError(null);
      void searchGatewaySessionCatalog(apiPort, normalized, controller.signal)
        .then((result) => {
          if (!controller.signal.aborted) {
            setSearchResults(result);
          }
        })
        .catch((error: unknown) => {
          if (!controller.signal.aborted) {
            setSearchError(errorMessage(error));
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) {
            setSearching(false);
          }
        });
    }, 250);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [apiPort, searchOpen, searchQuery]);

  useEffect(() => {
    if (!activeWorkspaceId || !currentSessionId || !navigation) {
      currentSessionRevealRequestRef.current += 1;
      currentSessionRevealKeyRef.current = null;
      setRevealError(null);
      return;
    }
    const revealKey = `${activeWorkspaceId}:${currentSessionId}`;
    if (currentSessionRevealKeyRef.current === revealKey) {
      return;
    }
    setRevealError(null);
    const currentSessionIsVisible = [...branchesRef.current.entries()].some(([key, branch]) =>
      key.startsWith(`${activeWorkspaceId}:`)
      && branch.items.some((item) => item.session_id === currentSessionId),
    );
    if (currentSessionIsVisible) {
      currentSessionRevealKeyRef.current = revealKey;
      return;
    }
    currentSessionRevealKeyRef.current = revealKey;
    const requestId = currentSessionRevealRequestRef.current + 1;
    currentSessionRevealRequestRef.current = requestId;
    void getSessionCatalogBreadcrumb(
      apiPort,
      workspaceRequestTarget(activeWorkspaceId),
      currentSessionId,
    )
      .then(async (breadcrumb) => {
        if (currentSessionRevealRequestRef.current !== requestId) {
          return;
        }
        await revealSearchResult(
          activeWorkspaceId,
          breadcrumb.items.map((item) => item.node_id),
          "session",
        );
      })
      .catch((error: unknown) => {
        if (currentSessionRevealRequestRef.current === requestId) {
          currentSessionRevealKeyRef.current = null;
          setRevealError(`定位当前会话失败: ${errorMessage(error)}`);
        }
      });
  }, [
    activeWorkspaceId,
    apiPort,
    currentSessionId,
    navigation,
    revealSearchResult,
    workspaceRequestTarget,
  ]);

  return useMemo(() => ({
    navigation,
    navigationError,
    revealError,
    branches: projectedBranches,
    expandedIds,
    searchResults,
    searching,
    searchError,
    generators,
    generationRuns,
    generatorError,
    refreshNavigation,
    refreshResourceTree,
    loadBranch,
    toggleExpanded,
    createWorkspaceFolder,
    revealSearchResult,
    revealWorkspaceFolder,
    createSessionFolder,
    renameWorkspaceFolder,
    placeWorkspaceNode,
    deleteWorkspaceFolder,
    renameSessionFolder,
    moveSessionFolder,
    deleteSessionFolder,
    moveCatalogNode,
    reconcileCatalogBranches,
    assignSessionFolder,
    createGenerator,
    refreshGenerationRuns,
    runGenerator,
    updateGenerator,
    deleteGenerator,
    previewGenerator,
  }), [
    projectedBranches,
    createGenerator,
    createSessionFolder,
    createWorkspaceFolder,
    deleteSessionFolder,
    deleteWorkspaceFolder,
    assignSessionFolder,
    expandedIds,
    generatorError,
    generators,
    generationRuns,
    loadBranch,
    navigation,
    navigationError,
    revealError,
    placeWorkspaceNode,
    moveSessionFolder,
    moveCatalogNode,
    previewGenerator,
    refreshGenerationRuns,
    refreshNavigation,
    refreshResourceTree,
    renameSessionFolder,
    renameWorkspaceFolder,
    revealSearchResult,
    reconcileCatalogBranches,
    runGenerator,
    searchError,
    searchResults,
    searching,
    toggleExpanded,
    updateGenerator,
    deleteGenerator,
  ]);
}

export type SessionResourceExplorerController = ReturnType<
  typeof useSessionResourceExplorer
>;
