import { getWorkspace } from "../../api/workspace";
import { fetchSessionCatalogSnapshot } from "../../api/session/sessionCatalogOperations";
import { getGatewayHealth } from "../../gatewayApi";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import type { GatewayUserAccess } from "../../types/backend";
import { errorMessage } from "../../utils/errorMessage";
import {
  catalogOutboxPartitionKey,
  catalogOutboxOperationIsTerminal,
  orderedCatalogOutboxOperations,
  type CatalogOutbox,
  type CatalogOutboxIntent,
  type CatalogOutboxOperation,
  type CatalogOutboxOptions,
  type CatalogOutboxPartition,
} from "../../state/session/sessionCatalogOutbox";
import {
  createIndexedDbCatalogOutboxPort,
  openCatalogOutboxDatabase,
  type CatalogOutboxPersistencePort,
} from "../../state/session/sessionCatalogOutboxStore";
import {
  createCatalogOutboxBroadcaster,
  createSessionCatalogOutboxDriver,
  type CatalogOutboxDispatchOutcome,
  type CatalogOutboxDriver,
  type SessionCatalogOperationsAdapter,
} from "./sessionCatalogOutboxDriver";
import { createSessionCatalogOperationId } from "./sessionCatalogOperationId";

export interface SessionCatalogOutboxEntry {
  gatewayWorkspaceId: string;
  principal: string;
  partitionKey: string;
  outbox: CatalogOutbox;
}

export type SessionCatalogOutboxSnapshot = ReadonlyMap<string, SessionCatalogOutboxEntry>;

export interface SessionCatalogOutboxHandle {
  gatewayWorkspaceId: string;
  workspaceTarget: GatewayWorkspaceRequestTarget;
  partition: CatalogOutboxPartition;
  partitionKey: string;
  driver: CatalogOutboxDriver;
}

export interface SessionCatalogFailureOwner {
  refresh: (
    workspaceTarget: GatewayWorkspaceRequestTarget,
    outbox: CatalogOutbox,
    minimumCatalogRevision: number,
    signal: AbortSignal,
  ) => Promise<void>;
  waitForProjection: (signal: AbortSignal) => Promise<void>;
}

export interface SessionCatalogOutboxRuntime {
  /** 只接入失败对账；打开、成功提交与原恢复入口不依赖 UI 注册。 */
  registerFailureOwner: (access: GatewayUserAccess, owner: SessionCatalogFailureOwner) => () => void;
  getSnapshot: () => SessionCatalogOutboxSnapshot;
  subscribe: (listener: () => void) => () => void;
  openWorkspace: (
    workspaceTarget: GatewayWorkspaceRequestTarget,
    access: GatewayUserAccess,
  ) => Promise<SessionCatalogOutboxHandle>;
  waitForTerminal: (
    handle: SessionCatalogOutboxHandle,
    operationId: string,
  ) => Promise<CatalogOutboxOperation>;
  submit: (
    workspaceTarget: GatewayWorkspaceRequestTarget,
    access: GatewayUserAccess,
    intent: CatalogOutboxIntent,
    intentOptions: CatalogOutboxOptions,
  ) => Promise<{
    handle: SessionCatalogOutboxHandle;
    operationId: string;
    dispatchOutcome: CatalogOutboxDispatchOutcome;
  }>;
  applyAndWait: (
    workspaceTarget: GatewayWorkspaceRequestTarget,
    access: GatewayUserAccess,
    intent: CatalogOutboxIntent,
    intentOptions: CatalogOutboxOptions,
  ) => Promise<{
    handle: SessionCatalogOutboxHandle;
    operationId: string;
    dispatchOutcome: CatalogOutboxDispatchOutcome;
    operation: CatalogOutboxOperation;
  }>;
  reportError: (message: string) => void;
  activatePrincipal: (access: GatewayUserAccess | null) => void;
  dispose: () => void;
}

interface SessionCatalogOutboxRuntimeOptions {
  apiPort: number;
  onError?: (message: string) => void;
  openPersistence?: () => Promise<CatalogOutboxPersistencePort>;
  adapter?: SessionCatalogOperationsAdapter;
  reconcileIntervalMs?: number;
}

const OPERATION_POLL_INTERVAL_MS = 750;
const RECONCILE_INTERVAL_MS = 1_000;

function waitForNextOperationPoll(): Promise<void> {
  return new Promise<void>((resolve) => {
    globalThis.setTimeout(resolve, OPERATION_POLL_INTERVAL_MS);
  });
}

interface RuntimeDriver extends SessionCatalogOutboxHandle {
  principal: string;
  generation: number;
  opening: Promise<void>;
  activeRequests: Set<AbortController>;
}

export function catalogOutboxWorkspaceScopeKey(
  gatewayWorkspaceId: string,
  principal: string,
): string {
  return `${gatewayWorkspaceId}\u0000${principal}`;
}

function principalFor(access: GatewayUserAccess): string {
  if (access.kind === "guest") return "guest";
  if (access.kind === "user" && access.user_id) return `user:${access.user_id}`;
  throw new Error(`Gateway 认证主体不支持会话目录 outbox: kind=${access.kind}`);
}

export function sessionCatalogOutboxScopeKey(
  gatewayWorkspaceId: string,
  access: GatewayUserAccess,
): string {
  return catalogOutboxWorkspaceScopeKey(gatewayWorkspaceId, principalFor(access));
}

function requireGatewayId(value: unknown): string {
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error("Gateway health 缺少稳定 gateway_id，拒绝建立会话目录 outbox 分区");
  }
  return value;
}

function requireBackendWorkspaceId(value: unknown): string {
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error("/api/v1/workspace 缺少后端 workspace UUID，拒绝建立会话目录 outbox 分区");
  }
  return value;
}

export function createSessionCatalogOutboxRuntime(
  options: SessionCatalogOutboxRuntimeOptions,
): SessionCatalogOutboxRuntime {
  const driversByScope = new Map<string, RuntimeDriver>();
  const openingsByScope = new Map<string, Promise<RuntimeDriver>>();
  const driversByPartition = new Map<string, RuntimeDriver>();
  const listeners = new Set<() => void>();
  const failureOwners = new Map<string, Set<SessionCatalogFailureOwner>>();
  const terminalOperations = new Map<string, CatalogOutboxOperation>();
  const reportedFailures = new Set<string>();
  let snapshot: SessionCatalogOutboxSnapshot = new Map();
  let gatewayIdPromise: Promise<string> | null = null;
  let persistencePromise: Promise<CatalogOutboxPersistencePort> | null = null;
  let broadcast: ReturnType<typeof createCatalogOutboxBroadcaster> | null = null;
  let unsubscribeBroadcast: (() => void) | null = null;
  let reconcileTimer: ReturnType<typeof globalThis.setInterval> | null = null;
  let activePrincipal: string | null = null;
  let principalGeneration = 0;
  let disposed = false;

  const reportError = (message: string): void => {
    if (disposed) return;
    options.onError?.(message);
  };

  const assertActivePrincipal = (principal: string, generation: number): void => {
    if (disposed || activePrincipal !== principal || principalGeneration !== generation) {
      throw new Error(
        `Gateway 认证主体已切换，拒绝继续使用会话目录 outbox: ${principal}`,
      );
    }
  };

  const isCurrentDriver = (runtimeDriver: RuntimeDriver): boolean =>
    !disposed
    && activePrincipal === runtimeDriver.principal
    && principalGeneration === runtimeDriver.generation
    && driversByScope.get(catalogOutboxWorkspaceScopeKey(
      runtimeDriver.gatewayWorkspaceId,
      runtimeDriver.principal,
    )) === runtimeDriver;

  const assertCurrentDriver = (runtimeDriver: RuntimeDriver): void => {
    assertActivePrincipal(runtimeDriver.principal, runtimeDriver.generation);
    if (!isCurrentDriver(runtimeDriver)) {
      throw new Error(
        `会话目录 outbox driver 已被身份切换替换: ${runtimeDriver.gatewayWorkspaceId}`,
      );
    }
  };

  const withRuntimeRequestSignal = async <T>(
    runtimeDriver: RuntimeDriver,
    request: (signal: AbortSignal) => Promise<T>,
  ): Promise<T> => {
    assertCurrentDriver(runtimeDriver);
    const controller = new AbortController();
    runtimeDriver.activeRequests.add(controller);
    try {
      const result = await request(controller.signal);
      assertCurrentDriver(runtimeDriver);
      return result;
    } catch (error: unknown) {
      assertCurrentDriver(runtimeDriver);
      throw error;
    } finally {
      runtimeDriver.activeRequests.delete(controller);
    }
  };

  const deactivateDriver = (runtimeDriver: RuntimeDriver): void => {
    runtimeDriver.driver.deactivate();
    for (const controller of runtimeDriver.activeRequests) controller.abort();
  };

  const activatePrincipal: SessionCatalogOutboxRuntime["activatePrincipal"] = (access) => {
    if (disposed) throw new Error("会话目录 outbox runtime 已停止");
    const nextPrincipal = access ? principalFor(access) : null;
    if (nextPrincipal === activePrincipal) return;
    activePrincipal = nextPrincipal;
    principalGeneration += 1;
    for (const [scopeKey, runtimeDriver] of driversByScope) {
      deactivateDriver(runtimeDriver);
      driversByScope.delete(scopeKey);
      if (driversByPartition.get(runtimeDriver.partitionKey) === runtimeDriver) {
        driversByPartition.delete(runtimeDriver.partitionKey);
      }
    }
  };

  const publish = (entry: SessionCatalogOutboxEntry): void => {
    const next = new Map(snapshot);
    next.set(
      catalogOutboxWorkspaceScopeKey(entry.gatewayWorkspaceId, entry.principal),
      entry,
    );
    snapshot = next;
    for (const listener of listeners) listener();
  };

  const startBroadcast = (): void => {
    if (broadcast) return;
    broadcast = createCatalogOutboxBroadcaster();
    unsubscribeBroadcast = broadcast.subscribe((partitionKey) => {
      const runtimeDriver = driversByPartition.get(partitionKey);
      if (!runtimeDriver || !isCurrentDriver(runtimeDriver)) return;
      void runtimeDriver.driver.reload().catch((error: unknown) => {
        if (isCurrentDriver(runtimeDriver)) {
          reportError(
            `重新读取会话目录本地 outbox 失败 (${runtimeDriver.gatewayWorkspaceId}): ${errorMessage(error)}`,
          );
        }
      });
    });
  };

  const scheduleReconciliation = (): void => {
    if (reconcileTimer !== null) return;
    reconcileTimer = globalThis.setInterval(() => {
      for (const runtimeDriver of driversByScope.values()) {
        if (!isCurrentDriver(runtimeDriver)) continue;
        const hasUnsettled = runtimeDriver.driver.current().operations.some(
          (operation) => operation.state !== "committed",
        );
        if (!hasUnsettled) continue;
        void runtimeDriver.driver.reconcile().catch((error: unknown) => {
          if (isCurrentDriver(runtimeDriver)) {
            reportError(
              `对账会话目录操作失败 (${runtimeDriver.gatewayWorkspaceId}): ${errorMessage(error)}`,
            );
          }
        });
      }
    }, options.reconcileIntervalMs ?? RECONCILE_INTERVAL_MS);
  };

  const getGatewayId = (): Promise<string> => {
    if (!gatewayIdPromise) {
      gatewayIdPromise = getGatewayHealth(options.apiPort)
        .then((health) => requireGatewayId(health.gateway_id));
    }
    return gatewayIdPromise;
  };

  const getPersistence = (): Promise<CatalogOutboxPersistencePort> => {
    persistencePromise ??= options.openPersistence
      ? options.openPersistence()
      : openCatalogOutboxDatabase().then(createIndexedDbCatalogOutboxPort);
    return persistencePromise;
  };

  const openWorkspace: SessionCatalogOutboxRuntime["openWorkspace"] = async (
    workspaceTarget,
    access,
  ) => {
    if (disposed) throw new Error("会话目录 outbox runtime 已停止");
    const gatewayWorkspaceId = workspaceTarget.workspace_id;
    if (!gatewayWorkspaceId) {
      throw new Error("建立会话目录 outbox 需要 Gateway workspace route ID");
    }
    const principal = principalFor(access);
    if (activePrincipal !== principal) {
      throw new Error(
        `Gateway 认证主体已切换，拒绝打开旧 principal outbox: requested=${principal}, active=${activePrincipal ?? "none"}`,
      );
    }
    const generation = principalGeneration;
    const scopeKey = catalogOutboxWorkspaceScopeKey(gatewayWorkspaceId, principal);
    const openingKey = `${scopeKey}\u0000${generation}`;
    const existing = driversByScope.get(scopeKey);
    if (existing && isCurrentDriver(existing)) {
      await existing.opening;
      assertCurrentDriver(existing);
      return existing;
    }
    const opening = openingsByScope.get(openingKey);
    if (opening) return await opening;

    const pending = (async (): Promise<RuntimeDriver> => {
      const [gatewayId, workspace] = await Promise.all([
        getGatewayId(),
        getWorkspace(options.apiPort, gatewayWorkspaceId),
      ]);
      assertActivePrincipal(principal, generation);
      const backendWorkspaceId = requireBackendWorkspaceId(workspace.workspace_id);
      if (workspaceTarget.backend_workspace_id
        && backendWorkspaceId !== workspaceTarget.backend_workspace_id) {
        throw new Error(
          `Gateway workspace backend 身份不匹配: route=${gatewayWorkspaceId}, `
          + `target=${workspaceTarget.backend_workspace_id}, actual=${backendWorkspaceId}`,
        );
      }
      const partition: CatalogOutboxPartition = {
        gatewayId,
        workspaceId: backendWorkspaceId,
        principal,
      };
      const partitionKey = catalogOutboxPartitionKey(partition);
      const currentByPartition = driversByPartition.get(partitionKey);
      if (currentByPartition && currentByPartition.gatewayWorkspaceId !== gatewayWorkspaceId) {
        throw new Error(
          `同一 outbox 分区映射到多个 Gateway workspace route: ${currentByPartition.gatewayWorkspaceId}, ${gatewayWorkspaceId}`,
        );
      }
      startBroadcast();
      const persistence = await getPersistence();
      assertActivePrincipal(principal, generation);
      let runtimeDriver!: RuntimeDriver;
      const driver = createSessionCatalogOutboxDriver({
        port: options.apiPort,
        gatewayWorkspaceId,
        workspaceTarget,
        partition,
        persistence,
        adapter: options.adapter,
        broadcast: broadcast?.broadcaster,
        async reconcileFailure(outbox, catalogRevision, finalize, signal) {
          assertCurrentDriver(runtimeDriver);
          const owners = [...(failureOwners.get(principal) ?? [])];
          if (owners.length === 0) {
            throw new Error("会话目录失败对账缺少 confirmed 分支刷新 owner，保留 pending 等待恢复");
          }
          await Promise.all(owners.map((owner) => owner.refresh(
            workspaceTarget, outbox, catalogRevision, signal,
          )));
          assertCurrentDriver(runtimeDriver);
          // 新 confirmed 必须先经过现有纯投影，才允许撤销失败闭包。
          await Promise.all(owners.map((owner) => owner.waitForProjection(signal)));
          assertCurrentDriver(runtimeDriver);
          if (owners.some((owner) => !failureOwners.get(principal)?.has(owner))) {
            throw new Error("会话目录 confirmed owner 已离开，保留 pending 等待恢复");
          }
          await finalize();
          // 清理后仍独立的 pending 必须已经由最新 confirmed 进入现有纯投影。
          await Promise.all(owners.map((owner) => owner.waitForProjection(signal)));
          assertCurrentDriver(runtimeDriver);
        },
        onChange(outbox) {
          for (const operation of outbox.operations) {
            if (catalogOutboxOperationIsTerminal(operation)) {
              const operationKey = `${partitionKey}\u0000${operation.client_operation_id}`;
              terminalOperations.set(
                operationKey,
                operation,
              );
              if (operation.state !== "committed"
                && isCurrentDriver(runtimeDriver)
                && !reportedFailures.has(operationKey)) {
                reportedFailures.add(operationKey);
                reportError(
                  `会话目录操作 ${operation.client_operation_id}未提交 (${operation.state})`
                  + `: ${operation.error_detail ?? operation.error_code ?? "后端未提供错误详情"}`,
                );
              }
            }
          }
          publish({ gatewayWorkspaceId, principal, partitionKey, outbox });
        },
      });
      runtimeDriver = {
        gatewayWorkspaceId,
        workspaceTarget,
        partition,
        partitionKey,
        driver,
        principal,
        generation,
        opening: Promise.resolve(),
        activeRequests: new Set(),
      };
      driversByScope.set(scopeKey, runtimeDriver);
      driversByPartition.set(partitionKey, runtimeDriver);
      runtimeDriver.opening = driver.restore().then(() => undefined);
      try {
        await runtimeDriver.opening;
        assertCurrentDriver(runtimeDriver);
      } catch (error: unknown) {
        deactivateDriver(runtimeDriver);
        if (driversByScope.get(scopeKey) === runtimeDriver) driversByScope.delete(scopeKey);
        if (driversByPartition.get(partitionKey) === runtimeDriver) {
          driversByPartition.delete(partitionKey);
        }
        throw error;
      }
      scheduleReconciliation();
      // 恢复只取回本地 pending；对账错误保持可见，不丢命令，也不阻止目录显示。
      void driver.reconcile().catch((error: unknown) => {
        if (isCurrentDriver(runtimeDriver)) {
          reportError(
            `恢复会话目录 outbox 对账失败 (${gatewayWorkspaceId}): ${errorMessage(error)}`,
          );
        }
      });
      return runtimeDriver;
    })();
    openingsByScope.set(openingKey, pending);
    try {
      return await pending;
    } finally {
      if (openingsByScope.get(openingKey) === pending) {
        openingsByScope.delete(openingKey);
      }
    }
  };

  const waitForTerminal: SessionCatalogOutboxRuntime["waitForTerminal"] = async (
    handle,
    operationId,
  ) => {
    const runtimeDriver = driversByScope.get(catalogOutboxWorkspaceScopeKey(
      handle.gatewayWorkspaceId,
      handle.partition.principal,
    ));
    if (!runtimeDriver || runtimeDriver.driver !== handle.driver) {
      throw new Error(
        `会话目录 outbox handle 已因身份切换失效: ${handle.gatewayWorkspaceId}`,
      );
    }
    const terminalKey = `${handle.partitionKey}\u0000${operationId}`;
    for (;;) {
      assertCurrentDriver(runtimeDriver);
      const terminal = terminalOperations.get(terminalKey)
        ?? handle.driver.current().operations.find(
          (operation) => operation.client_operation_id === operationId
            && catalogOutboxOperationIsTerminal(operation),
        );
      if (terminal) {
        if (terminal.state !== "committed") {
          throw new Error(
            `会话目录操作 ${operationId} 被 ${terminal.state}：`
            + `${terminal.error_code ?? "unknown"} ${terminal.error_detail ?? ""}`.trim(),
          );
        }
        if (terminal.committed_catalog_revision === null) {
          throw new Error(`已提交的会话目录操作缺少 committed_catalog_revision: ${operationId}`);
        }
        const catalog = await withRuntimeRequestSignal(runtimeDriver, (signal) =>
          fetchSessionCatalogSnapshot(
            options.apiPort,
            handle.workspaceTarget,
            { signal },
          ));
        if (catalog.catalog_revision >= terminal.committed_catalog_revision) {
          return terminal;
        }
      }

      await handle.driver.reconcile();
      assertCurrentDriver(runtimeDriver);
      const current = handle.driver.current().operations.find(
        (operation) => operation.client_operation_id === operationId,
      );
      if (!current && !terminalOperations.has(terminalKey)) {
        throw new Error(`会话目录操作已从 outbox 消失，缺少 terminal receipt: ${operationId}`);
      }
      await waitForNextOperationPoll();
      assertCurrentDriver(runtimeDriver);
    }
  };

  const submit: SessionCatalogOutboxRuntime["submit"] = async (
    workspaceTarget,
    access,
    intent,
    intentOptions,
  ) => {
    const handle = await openWorkspace(workspaceTarget, access);
    const operationId = createSessionCatalogOperationId();
    const targetOperations = intent.targetNodeId
      ? orderedCatalogOutboxOperations(handle.driver.current())
        .filter((operation) => operation.target_node_id === intent.targetNodeId
          && !catalogOutboxOperationIsTerminal(operation))
      : [];
    const pendingTarget = targetOperations[targetOperations.length - 1];
    const dependsOn = new Set(intentOptions.dependsOn ?? []);
    if (pendingTarget) dependsOn.add(pendingTarget.client_operation_id);
    if (intent.parentCreatedByOperationId) {
      dependsOn.add(intent.parentCreatedByOperationId);
    }
    const optionsWithDependencies = {
      ...intentOptions,
      dependsOn: [...dependsOn],
    };
    let dispatchOutcome: CatalogOutboxDispatchOutcome;
    try {
      dispatchOutcome = await handle.driver.applyIntent(
        operationId,
        intent,
        optionsWithDependencies,
      );
    } catch (error: unknown) {
      const currentDriver = driversByScope.get(catalogOutboxWorkspaceScopeKey(
        handle.gatewayWorkspaceId,
        handle.partition.principal,
      ));
      if (currentDriver?.driver === handle.driver && isCurrentDriver(currentDriver)) {
        reportError(
          `会话目录操作 ${operationId} 本地持久化或入队失败: ${errorMessage(error)}`,
        );
      }
      throw error;
    }
    if (dispatchOutcome === "persistence_failed") {
      throw new Error(`会话目录操作 ${operationId} 未能持久化`);
    }
    if (dispatchOutcome === "rejected") {
      throw new Error(`会话目录操作 ${operationId} 被后端明确拒绝`);
    }
    return { handle, operationId, dispatchOutcome };
  };

  const applyAndWait: SessionCatalogOutboxRuntime["applyAndWait"] = async (
    workspaceTarget,
    access,
    intent,
    intentOptions,
  ) => {
    const submitted = await submit(
      workspaceTarget,
      access,
      intent,
      intentOptions,
    );
    const operation = await waitForTerminal(submitted.handle, submitted.operationId);
    return { ...submitted, operation };
  };

  const registerFailureOwner: SessionCatalogOutboxRuntime["registerFailureOwner"] = (access, owner) => {
    if (disposed) throw new Error("会话目录 outbox runtime 已停止");
    const principal = principalFor(access);
    const owners = failureOwners.get(principal) ?? new Set<SessionCatalogFailureOwner>();
    owners.add(owner);
    failureOwners.set(principal, owners);
    // owner 晚于恢复就绪时重新对账现存命令，不给合法的打开入口增加注册前置。
    for (const runtimeDriver of driversByScope.values()) {
      if (runtimeDriver.principal !== principal || !isCurrentDriver(runtimeDriver)) continue;
      if (runtimeDriver.driver.current().operations.length === 0) continue;
      void runtimeDriver.driver.reconcile().catch((error: unknown) => {
        if (isCurrentDriver(runtimeDriver)) reportError("恢复失败对账: " + errorMessage(error));
      });
    }
    return () => {
      owners.delete(owner);
      if (owners.size === 0) failureOwners.delete(principal);
    };
  };

  const dispose: SessionCatalogOutboxRuntime["dispose"] = () => {
    if (disposed) return;
    disposed = true;
    activePrincipal = null;
    principalGeneration += 1;
    if (reconcileTimer !== null) globalThis.clearInterval(reconcileTimer);
    reconcileTimer = null;
    for (const runtimeDriver of driversByScope.values()) deactivateDriver(runtimeDriver);
    driversByScope.clear();
    driversByPartition.clear();
    openingsByScope.clear();
    failureOwners.clear();
    unsubscribeBroadcast?.();
    unsubscribeBroadcast = null;
    broadcast?.close();
    broadcast = null;
  };

  return {
    registerFailureOwner,
    getSnapshot: () => snapshot,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    openWorkspace,
    waitForTerminal,
    submit,
    applyAndWait,
    reportError,
    activatePrincipal,
    dispose,
  };
}
