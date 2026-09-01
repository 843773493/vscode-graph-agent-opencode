import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  createSessionCatalogFolder,
  assignSessionCatalogFolder,
  deleteSessionCatalogFolder,
  getSession,
  getSessionCatalogBreadcrumb,
  listSessionCatalogChildren,
  moveSessionCatalogNode,
  moveSessionCatalogFolder,
  refreshSessionCatalog,
  renameSessionCatalogFolder,
} from "../api";
import {
  createWorkspaceNavigationFolder,
  deleteWorkspaceNavigationFolder,
  getWorkspaceNavigation,
  placeWorkspaceNavigationNode,
  renameWorkspaceNavigationFolder,
  searchGatewaySessionCatalog,
} from "../gatewayApi";
import type {
  GatewaySessionSearchResults,
  SessionCatalogPage,
  WorkspaceNavigationTree,
} from "../types/backend";
import type { SessionGeneratorResourcesController } from "./sessionResourceExplorer/useSessionGeneratorResources";
import { changedCatalogWorkspaceIds } from "./sessionResourceExplorer/resourceTreeSync";

export interface CatalogBranchState extends SessionCatalogPage {
  loading: boolean;
  error: string | null;
}

const emptySearch: GatewaySessionSearchResults = {
  items: [],
  workspaces: [],
  total: 0,
};

const CATALOG_RETRY_LIMIT = 3;

function isRetryableCatalogError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return /(?:HTTP\s*(?:401|502|503|504)|401\s+Unauthorized|Failed to fetch|NetworkError|ERR_NETWORK_CHANGED|连接被拒绝|暂时不可用)/i.test(
    message,
  );
}

function catalogRetryDelay(attempt: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, 250 * (attempt + 1));
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

export function useSessionResourceExplorer({
  apiPort,
  activeWorkspaceId,
  searchOpen,
  searchQuery,
  currentSessionId,
  workspaceNavigationSyncKey,
  catalogSyncKeys,
  catalogRefreshVersions,
  generatorResources,
}: {
  apiPort: number;
  activeWorkspaceId: string | null;
  searchOpen: boolean;
  searchQuery: string;
  currentSessionId: string;
  workspaceNavigationSyncKey: string;
  catalogSyncKeys: ReadonlyMap<string, string>;
  catalogRefreshVersions: ReadonlyMap<string, number>;
  generatorResources: SessionGeneratorResourcesController;
}) {
  const [navigation, setNavigation] = useState<WorkspaceNavigationTree | null>(null);
  const navigationRef = useRef(navigation);
  navigationRef.current = navigation;
  const [navigationError, setNavigationError] = useState<string | null>(null);
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
  const catalogSyncKeysRef = useRef(catalogSyncKeys);
  const catalogRefreshVersionsRef = useRef(catalogRefreshVersions);
  const generationOutputSyncKeysRef = useRef<ReadonlyMap<string, string>>(new Map());

  const refreshNavigation = useCallback(async () => {
    const requestId = navigationRequestRef.current + 1;
    navigationRequestRef.current = requestId;
    try {
      const next = await getWorkspaceNavigation(apiPort);
      if (navigationRequestRef.current === requestId) {
        setNavigation(next);
        setNavigationError(null);
      }
      return next;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (navigationRequestRef.current === requestId) {
        setNavigationError(message);
      }
      throw error;
    }
  }, [apiPort]);

  const loadBranch = useCallback(async (
    workspaceId: string,
    parentNodeId?: string | null,
    append = false,
  ) => {
    const key = branchKey(workspaceId, parentNodeId);
    const requestId = (branchRequestRefs.current.get(key) ?? 0) + 1;
    branchRequestRefs.current.set(key, requestId);
    const current = branchesRef.current.get(key);
    setBranches((previous) => {
      const next = new Map(previous);
      next.set(key, {
        revision: current?.revision ?? "",
        parent_node_id: parentNodeId ?? null,
        items: current?.items ?? [],
        cursor: current?.cursor ?? null,
        total: current?.total ?? 0,
        loading: true,
        error: null,
      });
      return next;
    });
    try {
      let page: SessionCatalogPage;
      for (let attempt = 0; ; attempt += 1) {
        try {
          page = await listSessionCatalogChildren(
            apiPort,
            workspaceId,
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
        return;
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
    } catch (error) {
      if (branchRequestRefs.current.get(key) !== requestId) {
        return;
      }
      const message = error instanceof Error ? error.message : String(error);
      setBranches((previous) => {
        const next = new Map(previous);
        const previousBranch = next.get(key);
        next.set(key, {
          revision: previousBranch?.revision ?? "",
          parent_node_id: parentNodeId ?? null,
          items: previousBranch?.items ?? [],
          cursor: previousBranch?.cursor ?? null,
          total: previousBranch?.total ?? 0,
          loading: false,
          error: message,
        });
        return next;
      });
      throw error;
    }
  }, [apiPort]);

  const refreshCatalogWorkspace = useCallback(async (workspaceId: string) => {
    const parentNodeIds = new Set<string | null>();
    for (const [key, branch] of branchesRef.current.entries()) {
      if (key.startsWith(`${workspaceId}:`)) {
        parentNodeIds.add(branch.parent_node_id ?? null);
      }
    }
    if (workspaceId === activeWorkspaceId || parentNodeIds.size > 0) {
      parentNodeIds.add(null);
    }
    await Promise.all(
      [...parentNodeIds].map((parentNodeId) =>
        loadBranch(workspaceId, parentNodeId),
      ),
    );
  }, [activeWorkspaceId, loadBranch]);

  const refreshResourceTree = useCallback(async () => {
    const navigationPromise = refreshNavigation();
    const catalogPromise = activeWorkspaceId
      ? (async () => {
          const rootKey = branchKey(activeWorkspaceId, null);
          const requestId = (branchRequestRefs.current.get(rootKey) ?? 0) + 1;
          branchRequestRefs.current.set(rootKey, requestId);
          const rootPage = await refreshSessionCatalog(apiPort, activeWorkspaceId);
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
  }, [activeWorkspaceId, apiPort, loadBranch, refreshNavigation]);

  const loadBranchUntilNode = useCallback(async (
    workspaceId: string,
    parentNodeId: string | null,
    targetNodeId: string,
  ) => {
    const key = branchKey(workspaceId, parentNodeId);
    const loaded = branchesRef.current.get(key);
    if (loaded?.items.some((item) => item.node_id === targetNodeId)) {
      return;
    }
    setBranches((previous) => {
      const next = new Map(previous);
      next.set(key, {
        revision: loaded?.revision ?? "",
        parent_node_id: parentNodeId,
        items: loaded?.items ?? [],
        cursor: loaded?.cursor ?? null,
        total: loaded?.total ?? 0,
        loading: true,
        error: null,
      });
      return next;
    });
    const items: SessionCatalogPage["items"] = [];
    const visitedCursors = new Set<string>();
    let cursor: string | null = null;
    try {
      while (true) {
        const page = await listSessionCatalogChildren(
          apiPort,
          workspaceId,
          parentNodeId,
          cursor,
        );
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
      const message = error instanceof Error ? error.message : String(error);
      setBranches((previous) => {
        const next = new Map(previous);
        const branch = next.get(key);
        next.set(key, {
          revision: branch?.revision ?? "",
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
  }, [apiPort]);

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
    if (navigationRequestRef.current === requestId) {
      setNavigation(next);
      setNavigationError(null);
    }
  }, [apiPort]);

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

  const createSessionFolder = useCallback(async (
    workspaceId: string,
    name: string,
    parentFolderId?: string | null,
  ) => {
    await createSessionCatalogFolder(apiPort, workspaceId, name, parentFolderId);
    await loadBranch(workspaceId, parentFolderId);
  }, [apiPort, loadBranch]);

  const resolveSession = useCallback(async (workspaceId: string, sessionId: string) => {
    return getSession(apiPort, sessionId, workspaceId);
  }, [apiPort]);

  const renameWorkspaceFolder = useCallback(async (nodeId: string, name: string) => {
    const requestId = navigationRequestRef.current + 1;
    navigationRequestRef.current = requestId;
    const next = await renameWorkspaceNavigationFolder(apiPort, nodeId, name);
    if (navigationRequestRef.current === requestId) {
      setNavigation(next);
      setNavigationError(null);
    }
  }, [apiPort]);

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
      if (navigationRequestRef.current === requestId) {
        setNavigation(next);
        setNavigationError(null);
      }
    } catch (operationError) {
      try {
        await refreshNavigation();
      } catch (reconciliationError) {
        throw new Error(
          `${operationError instanceof Error ? operationError.message : String(operationError)}；重新读取工作区导航也失败: ${reconciliationError instanceof Error ? reconciliationError.message : String(reconciliationError)}`,
        );
      }
      throw operationError;
    }
  }, [apiPort, refreshNavigation]);

  const deleteWorkspaceFolder = useCallback(async (nodeId: string) => {
    const requestId = navigationRequestRef.current + 1;
    navigationRequestRef.current = requestId;
    const next = await deleteWorkspaceNavigationFolder(apiPort, nodeId);
    if (navigationRequestRef.current === requestId) {
      setNavigation(next);
      setNavigationError(null);
    }
  }, [apiPort]);

  const renameSessionFolder = useCallback(async (
    workspaceId: string,
    folderId: string,
    name: string,
    parentFolderId?: string | null,
  ) => {
    await renameSessionCatalogFolder(apiPort, workspaceId, folderId, name);
    await loadBranch(workspaceId, parentFolderId);
  }, [apiPort, loadBranch]);

  const moveSessionFolder = useCallback(async (
    workspaceId: string,
    folderId: string,
    parentFolderId?: string | null,
    previousParentId?: string | null,
  ) => {
    await moveSessionCatalogFolder(apiPort, workspaceId, folderId, parentFolderId);
    await Promise.all([
      loadBranch(workspaceId, previousParentId),
      parentFolderId !== previousParentId
        ? loadBranch(workspaceId, parentFolderId)
        : Promise.resolve(),
    ]);
  }, [apiPort, loadBranch]);

  const deleteSessionFolder = useCallback(async (
    workspaceId: string,
    folderId: string,
    parentFolderId?: string | null,
  ) => {
    let deletedCurrentSession = false;
    if (workspaceId === activeWorkspaceId && currentSessionId) {
      const breadcrumb = await getSessionCatalogBreadcrumb(
        apiPort,
        workspaceId,
        currentSessionId,
      );
      deletedCurrentSession = breadcrumb.items.some(
        (item) => item.node_id === folderId,
      );
    }
    await deleteSessionCatalogFolder(apiPort, workspaceId, folderId);
    await loadBranch(workspaceId, parentFolderId);
    return deletedCurrentSession;
  }, [activeWorkspaceId, apiPort, currentSessionId, loadBranch]);

  const reconcileCatalogBranches = useCallback(async (
    workspaceId: string,
    parentNodeIds: Array<string | null>,
  ) => {
    const errors: string[] = [];
    for (const parentNodeId of new Set(parentNodeIds)) {
      try {
        await loadBranch(workspaceId, parentNodeId);
      } catch (error) {
        errors.push(
          `${parentNodeId ?? "root"}: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
    }
    if (errors.length > 0) {
      throw new Error(`重新读取会话目录失败: ${errors.join("；")}`);
    }
  }, [loadBranch]);

  const moveCatalogNode = useCallback(async (
    workspaceId: string,
    nodeId: string,
    parentNodeId: string | null,
    previousParentNodeId: string | null,
  ) => {
    try {
      await moveSessionCatalogNode(apiPort, workspaceId, nodeId, parentNodeId);
    } catch (operationError) {
      try {
        await reconcileCatalogBranches(
          workspaceId,
          [previousParentNodeId, parentNodeId],
        );
      } catch (reconciliationError) {
        throw new Error(
          `${operationError instanceof Error ? operationError.message : String(operationError)}；${reconciliationError instanceof Error ? reconciliationError.message : String(reconciliationError)}`,
        );
      }
      throw operationError;
    }
    await reconcileCatalogBranches(
      workspaceId,
      [previousParentNodeId, parentNodeId],
    );
  }, [apiPort, reconcileCatalogBranches]);

  const assignSessionFolder = useCallback(async (
    workspaceId: string,
    sessionId: string,
    folderId?: string | null,
    previousParentId?: string | null,
  ) => {
    await assignSessionCatalogFolder(apiPort, workspaceId, sessionId, folderId);
    await Promise.all([
      loadBranch(workspaceId, previousParentId),
      folderId !== previousParentId
        ? loadBranch(workspaceId, folderId)
        : Promise.resolve(),
    ]);
  }, [apiPort, loadBranch]);

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
      changedWorkspaceIds.map(refreshCatalogWorkspace),
    ).catch(() => {
      // 每个失败分支都保留自己的可见错误，不能提升为整个资源树故障。
    });
  }, [catalogRefreshVersions, catalogSyncKeys, refreshCatalogWorkspace]);

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
    void Promise.all(changedWorkspaceIds.map(refreshCatalogWorkspace))
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
            setSearchError(error instanceof Error ? error.message : String(error));
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
      return;
    }
    const revealKey = `${activeWorkspaceId}:${currentSessionId}`;
    if (currentSessionRevealKeyRef.current === revealKey) {
      return;
    }
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
    void getSessionCatalogBreadcrumb(apiPort, activeWorkspaceId, currentSessionId)
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
          setNavigationError(
            `定位当前会话失败: ${error instanceof Error ? error.message : String(error)}`,
          );
        }
      });
  }, [
    activeWorkspaceId,
    apiPort,
    currentSessionId,
    navigation,
    revealSearchResult,
  ]);

  return useMemo(() => ({
    navigation,
    navigationError,
    branches,
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
    resolveSession,
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
    branches,
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
    placeWorkspaceNode,
    moveSessionFolder,
    moveCatalogNode,
    previewGenerator,
    refreshGenerationRuns,
    refreshNavigation,
    refreshResourceTree,
    resolveSession,
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
