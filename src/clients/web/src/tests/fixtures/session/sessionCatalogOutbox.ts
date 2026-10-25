import {
  addCatalogOutboxIntent,
  applyCatalogOutboxReceipts,
  assignCatalogOutboxOperationSequence,
  createCatalogOutbox,
  markCatalogOutboxOperationPersisted,
  pruneReconciledCatalogOutbox,
  resolveCatalogOutboxFailures,
  type CatalogOutboxIntent,
  type CatalogOutboxOperation,
  type CatalogOutboxOptions,
} from "../../../state/session/sessionCatalogOutbox";
import type {
  CatalogOutboxDispatchOutcome,
  CatalogOutboxDriver,
} from "../../../hooks/session/sessionCatalogOutboxDriver";
import type {
  SessionCatalogFailureOwner,
  SessionCatalogOutboxHandle,
  SessionCatalogOutboxRuntime,
  SessionCatalogOutboxSnapshot,
} from "../../../hooks/session/sessionCatalogOutboxRuntime";
import { errorMessage } from "../../../utils/errorMessage";

export interface TestCatalogMutationResult {
  state?: "committed" | "rejected" | "cancelled" | "dependency_failed";
  settlement?: CatalogOutboxOperation["settlement"];
  errorCode?: string;
  errorDetail?: string;
}

export interface SessionCatalogOutboxTestRuntime {
  runtime: SessionCatalogOutboxRuntime;
  errors: string[];
  pruneCalls: Array<{ catalogRevision: number; operationIds: readonly string[] | undefined }>;
  submitted: Array<{
    workspaceId: string;
    intent: CatalogOutboxIntent;
    options: CatalogOutboxOptions;
  }>;
}

/** 生命周期与资源树 hook 的确定性 outbox 桩；实际状态转移仍复用生产纯状态机。 */
export function createSessionCatalogOutboxTestRuntime(options: {
  result?: (intent: CatalogOutboxIntent) => TestCatalogMutationResult;
  dispatchOutcome?: CatalogOutboxDispatchOutcome;
} = {}): SessionCatalogOutboxTestRuntime {
  const listeners = new Set<() => void>();
  const handles = new Map<string, SessionCatalogOutboxHandle>();
  const failureOwners = new Map<string, Set<SessionCatalogFailureOwner>>();
  const terminalOperations = new Map<string, CatalogOutboxOperation>();
  const entries = new Map<string, {
    gatewayWorkspaceId: string;
    principal: string;
    partitionKey: string;
    outbox: ReturnType<typeof createCatalogOutbox>;
  }>();
  const errors: string[] = [];
  const pruneCalls: SessionCatalogOutboxTestRuntime["pruneCalls"] = [];
  const submitted: SessionCatalogOutboxTestRuntime["submitted"] = [];
  let nextOperationId = 1;
  let snapshot: SessionCatalogOutboxSnapshot = new Map();

  const publish = (handle: SessionCatalogOutboxHandle, outbox: ReturnType<typeof createCatalogOutbox>) => {
    const key = `${handle.gatewayWorkspaceId}\u0000${handle.partition.principal}`;
    entries.set(key, {
      gatewayWorkspaceId: handle.gatewayWorkspaceId,
      principal: handle.partition.principal,
      partitionKey: handle.partitionKey,
      outbox,
    });
    snapshot = new Map(entries);
    for (const listener of listeners) listener();
  };

  const openWorkspace: SessionCatalogOutboxRuntime["openWorkspace"] = async (
    workspaceTarget,
    access,
  ) => {
    const gatewayWorkspaceId = workspaceTarget.workspace_id;
    const backendWorkspaceId = workspaceTarget.backend_workspace_id;
    if (!backendWorkspaceId) throw new Error("测试 Gateway workspace target 缺少 backend_workspace_id");
    const principal = access.kind === "guest"
      ? "guest"
      : access.user_id ? `user:${access.user_id}` : "";
    if (!principal) throw new Error("测试 outbox 缺少认证主体");
    const scope = `${gatewayWorkspaceId}\u0000${principal}`;
    const existing = handles.get(scope);
    if (existing) return existing;

    let outbox = createCatalogOutbox({
      gatewayId: "gateway-test",
      workspaceId: backendWorkspaceId,
      principal,
    });
    let handle!: SessionCatalogOutboxHandle;
    const driver: CatalogOutboxDriver = {
      restore: async () => outbox,
      reload: async () => outbox,
      current: () => outbox,
      applyIntent: async (operationId, intent, intentOptions) => {
        outbox = addCatalogOutboxIntent(outbox, operationId, intent, intentOptions);
        const sequence = outbox.operations.length;
        outbox = assignCatalogOutboxOperationSequence(outbox, operationId, sequence);
        outbox = markCatalogOutboxOperationPersisted(outbox, operationId);
        const result = options.result?.(intent) ?? {};
        const state = result.state ?? "committed";
        outbox = applyCatalogOutboxReceipts(outbox, [{
          operation_id: operationId,
          client_sequence: sequence,
          queue_seq: sequence,
          kind: intent.kind,
          state,
          created_node_id: intent.kind === "create_folder" ? `fld_${operationId}` : null,
          committed_catalog_revision: state === "committed"
            ? intentOptions.baseCatalogRevision + 1
            : null,
          error_code: result.errorCode ?? (state === "committed" ? null : "test_rejection"),
          error_detail: result.errorDetail ?? null,
          settlement: result.settlement ?? (state === "committed"
            && (intent.kind === "delete_folder" || intent.kind === "delete_session") ? "settled" : null),
          receipt_revision: sequence,
          updated_at: "2026-10-04T00:00:00Z",
        }]);
        const terminal = outbox.operations.find((item) => item.client_operation_id === operationId);
        if (terminal) terminalOperations.set(operationId, terminal);
        publish(handle, outbox);
        return { dispatch: Promise.resolve(options.dispatchOutcome ?? "accepted") };
      },
      reconcile: async () => {
        const resolution = resolveCatalogOutboxFailures(outbox);
        if (resolution.removed_operation_ids.length === 0) return outbox;
        const owners = [...(failureOwners.get(principal) ?? [])];
        if (owners.length === 0) throw new Error("测试 outbox 缺少 confirmed 分支刷新 owner，保留失败项");
        const signal = new AbortController().signal;
        await Promise.all(owners.map((owner) => owner.refresh(workspaceTarget, outbox, 0, signal)));
        await Promise.all(owners.map((owner) => owner.waitForProjection(signal)));
        outbox = resolveCatalogOutboxFailures(outbox).outbox;
        publish(handle, outbox);
        await Promise.all(owners.map((owner) => owner.waitForProjection(signal)));
        return outbox;
      },
      deactivate: () => undefined,
      prune: async (catalogRevision, operationIds) => {
        pruneCalls.push({ catalogRevision, operationIds });
        outbox = pruneReconciledCatalogOutbox(outbox, catalogRevision, operationIds);
        publish(handle, outbox);
        return outbox;
      },
    };
    const initialOutbox = outbox;
    handle = {
      gatewayWorkspaceId,
      workspaceTarget,
      partition: initialOutbox.partition,
      partitionKey: `${initialOutbox.partition.gatewayId}\u0000${initialOutbox.partition.workspaceId}\u0000${initialOutbox.partition.principal}`,
      driver,
    };
    handles.set(scope, handle);
    publish(handle, outbox);
    return handle;
  };

  const submit: SessionCatalogOutboxRuntime["submit"] = async (
    workspaceTarget,
    access,
    intent,
    intentOptions,
  ) => {
    const handle = await openWorkspace(workspaceTarget, access);
    const operationId = `test_catalog_operation_${nextOperationId++}`;
    submitted.push({ workspaceId: workspaceTarget.workspace_id, intent, options: intentOptions });
    const { dispatch } = await handle.driver.applyIntent(operationId, intent, intentOptions);
    void dispatch.catch((error: unknown) => errors.push(String(error)));
    return {
      handle,
      operationId,
    };
  };

  const waitForTerminal: SessionCatalogOutboxRuntime["waitForTerminal"] = async (
    handle,
    operationId,
  ) => {
    const operation = terminalOperations.get(operationId) ?? handle.driver.current().operations.find(
      (item) => item.client_operation_id === operationId,
    );
    if (!operation) throw new Error(`测试 outbox 缺少 operation: ${operationId}`);
    if (operation.state !== "committed") {
      let refreshError = "";
      try { await handle.driver.reconcile(); }
      catch (error) { refreshError = "；" + errorMessage(error); }
      throw new Error(
        `会话目录操作 ${operationId} 被 ${operation.state}：`
        + `${operation.error_code ?? "unknown"} ${operation.error_detail ?? ""}`.trim() + refreshError,
      );
    }
    if (operation.settlement === "repair_required" || operation.settlement === "pending") {
      throw new Error(`删除已提交但排空未完成: ${operation.error_detail ?? operation.settlement}`);
    }
    return operation;
  };

  const runtime: SessionCatalogOutboxRuntime = {
    registerFailureOwner(access, owner) {
      const principal = access.kind === "guest" ? "guest" : `user:${access.user_id}`;
      const owners = failureOwners.get(principal) ?? new Set<SessionCatalogFailureOwner>();
      owners.add(owner);
      failureOwners.set(principal, owners);
      return () => {
        owners.delete(owner);
        if (owners.size === 0) failureOwners.delete(principal);
      };
    },
    getSnapshot: () => snapshot,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    openWorkspace,
    waitForTerminal,
    submit,
    applyAndWait: async (...args) => {
      const result = await submit(...args);
      const operation = await waitForTerminal(result.handle, result.operationId);
      return { ...result, operation };
    },
    reportError(message) {
      errors.push(message);
    },
    activatePrincipal() {},
    dispose() { failureOwners.clear(); },
  };

  return { runtime, errors, pruneCalls, submitted };
}
