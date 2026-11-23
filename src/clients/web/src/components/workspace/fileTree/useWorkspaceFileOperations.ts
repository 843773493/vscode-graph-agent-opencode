import { useCallback, useEffect, useRef, useState } from "react";
import { t } from "../../../../../shared/core/i18n/index.js";
import {
  getWorkspace,
  getWorkspaceFileRoots,
  getWorkspaceFileOperations,
  getWorkspaceFiles,
  resolveWorkspaceFileReferences,
} from "../../../api";
import { gatewayWorkspaceKey, workspaceOwnerFromKey } from "../../../api/workspaceIdentity";
import { NAVIGATION_MUTATION_TERMINAL_STATES } from "../../../api/session/sessionCatalogOperations";
import { dispatchWorkspaceFileIntent, WorkspaceFileIntentRejectedError } from "../../../state/workspaceFiles/dispatcher";
import type { WorkspaceDirectoryKey, WorkspaceDirectoryRevision, WorkspaceFileList, WorkspaceFileOperationReceipt } from "../../../types/backend";
import {
  openWorkspaceFileIntentStore,
  type FileIntentCommand,
  type FileIntentScope,
  type StoredFileIntent,
  type WorkspaceFileIntentStore,
} from "../../../state/workspaceFiles/intentStore";
import { errorMessage, isErrorReported } from "../../../utils/errorMessage";
import { localDatabaseError } from "../../../state/browserPersistence";

interface FileOperationOptions {
  port: number;
  workspaceId: string | null;
  reconcileDirectories: (directories: readonly WorkspaceDirectoryRevision[], forcePath?: string) => Promise<void>;
  acceptSnapshots: (snapshots: readonly WorkspaceFileList[], forcePath?: string) => void;
  directoryVersion: (path: string) => { key: WorkspaceDirectoryKey; revision: number } | null;
  onStatusChange: (message: string) => void;
  onError: (message: string) => void;
  onCommitted: (directoryPath: string) => void;
}

interface Driver {
  scope: FileIntentScope;
  store: WorkspaceFileIntentStore;
}

export interface PendingFileCreation {
  operationId: string;
  clientRef: string;
  directoryPath: string;
  name: string;
  state: string;
}

/** 四入口共享一个恢复循环；只派发 IndexedDB 已冻结的原请求。 */
export function useWorkspaceFileOperations(options: FileOperationOptions) {
  const { port, workspaceId } = options;
  const callbacks = useRef(options);
  callbacks.current = options;
  const [pendingCreations, setPendingCreations] = useState<PendingFileCreation[]>([]);
  const driver = useRef<Promise<Driver> | null>(null);
  const generation = useRef(0);
  const pump = useRef<Promise<void> | null>(null);
  const pumpController = useRef<AbortController | null>(null);

  const open = useCallback(async (): Promise<Driver> => {
    if (!workspaceId) throw new Error(t("fileTree.operation.requiresWorkspace"));
    if (!driver.current) {
      driver.current = (async () => {
        const [backendWorkspace, fileRoots, store] = await Promise.all([
          getWorkspace(port, workspaceId), getWorkspaceFileRoots(port, workspaceId),
          openWorkspaceFileIntentStore(),
        ]);
        const owner = workspaceOwnerFromKey(workspaceId);
        if (backendWorkspace.workspace_id !== owner.workspaceId || !fileRoots.workspace_uri) {
          throw new Error(t("fileTree.operation.missingOwner"));
        }
        return { store, scope: { ...owner, workspaceRoot: fileRoots.workspace_uri, actor: "local" } };
      })();
      const opening = driver.current;
      void opening.catch(() => { if (driver.current === opening) driver.current = null; });
    }
    return await driver.current;
  }, [port, workspaceId]);

  const run = useCallback((): Promise<void> => {
    if (pump.current) return pump.current;
    const epoch = generation.current;
    const controller = new AbortController();
    pumpController.current = controller;
    const current = () => epoch === generation.current;
    const task = (async () => {
      const target = await open();
      if (!current()) return;
      const records = await target.store.load(target.scope);
      const oldOwners = (await target.store.loadForWorkspace(target.scope))
        .filter((record) => record.scopeKey !== records[0]?.scopeKey
          && (record.scope.gatewayId !== target.scope.gatewayId
            || record.scope.workspaceId !== target.scope.workspaceId
            || record.scope.workspaceRoot !== target.scope.workspaceRoot));
      if (oldOwners.length > 0) callbacks.current.onError(t("fileTree.operation.pendingOldOwner"));
      if (records.length === 0) { setPendingCreations([]); return; }
      // 查询与重试前重新核固定 route，Gateway 或工作区重连不能改投原 intent。
      const backendWorkspace = await getWorkspace(port, gatewayWorkspaceKey(target.scope));
      if (!current()) return;
      if (backendWorkspace.workspace_id !== target.scope.workspaceId) {
        throw new Error(t("fileTree.operation.ownerChanged"));
      }
      const updatePending = (values: StoredFileIntent[]) => {
        if (!current()) return;
        setPendingCreations(values.flatMap((record) => record.command.kind === "create"
          && record.command.entryKind === "directory" ? [{
              operationId: record.intent.client_operation_id, clientRef: record.intent.client_ref ?? "",
              directoryPath: record.directoryPath, name: record.command.name,
              state: record.receipt?.state ?? "pending_local",
            }] : []));
      };
      updatePending(records);
      const blocked = new Set<string>();
      const receipts = new Map(records.map((record) => [record.intent.client_operation_id, record.receipt]));
      for (const record of records) {
        if (!current()) return;
        if (record.intent.depends_on.some((id) => blocked.has(id)
          || (receipts.has(id) && receipts.get(id)?.state !== "committed"))) {
          blocked.add(record.intent.client_operation_id);
          continue;
        }
        try {
        let receipt: WorkspaceFileOperationReceipt;
        try { receipt = await dispatchWorkspaceFileIntent(port, record, controller.signal); }
        catch (error) {
          if (error instanceof WorkspaceFileIntentRejectedError && current()) {
            await callbacks.current.reconcileDirectories([], record.directoryPath);
            if (!current()) return;
            await target.store.remove(record);
          }
          throw error;
        }
        if (!current()) return;
        await target.store.accept(record, receipt);
        receipts.set(record.intent.client_operation_id, receipt);
        if (!current()) return;
        if (NAVIGATION_MUTATION_TERMINAL_STATES.includes(receipt.state)) {
          callbacks.current.acceptSnapshots(receipt.snapshots, record.directoryPath);
          if (receipt.state === "committed") {
            await callbacks.current.reconcileDirectories(receipt.directories, record.directoryPath);
          } else {
            await callbacks.current.reconcileDirectories(receipt.directories);
            await callbacks.current.reconcileDirectories([], record.directoryPath);
          }
          if (!current()) return;
          await target.store.remove(record);
          if (receipt.state === "committed") {
            callbacks.current.onCommitted(record.directoryPath);
            callbacks.current.onStatusChange(t("fileTree.operation.completed"));
          }
          else callbacks.current.onError(t("fileTree.operation.failed", receipt.effect, receipt.error_detail ?? receipt.state));
        } else {
          callbacks.current.onStatusChange(t("fileTree.operation.accepted", receipt.state === "queued" ? t("fileTree.operation.queued") : t("fileTree.operation.running")));
        }
        } catch (error) {
          blocked.add(record.intent.client_operation_id);
          if (current()) callbacks.current.onError(errorMessage(error));
        }
      }
      updatePending(await target.store.load(target.scope));
    })();
    pump.current = task;
    void task.finally(() => {
      if (pump.current === task) { pump.current = null; pumpController.current = null; }
    }).catch(() => undefined);
    return task;
  }, [open, port]);

  useEffect(() => {
    pumpController.current?.abort();
    generation.current += 1;
    driver.current = null;
    pump.current = null;
    setPendingCreations([]);
    if (!workspaceId || typeof indexedDB === "undefined") return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const store = await openWorkspaceFileIntentStore();
        if ((await store.loadForWorkspace(workspaceOwnerFromKey(workspaceId))).length > 0) await run();
      }
      catch (error) {
        if (!stopped && !isErrorReported(error)) callbacks.current.onError(errorMessage(error));
        // 存储故障需明确恢复，不能每 500ms 重开同一个受阻数据库。
        if (localDatabaseError(error)) return;
      }
      if (!stopped) timer = setTimeout(() => void poll(), 500);
    };
    void poll();
    return () => { stopped = true; generation.current += 1; pumpController.current?.abort(); clearTimeout(timer); };
  }, [run, workspaceId]);

  const submit = useCallback(async (directoryPath: string, command: FileIntentCommand,
    createdByOperationId?: string): Promise<void> => {
    const epoch = generation.current;
    const target = await open();
    if (epoch !== generation.current) return;
    let directoryKey;
    let revision: number | null = null;
    if (createdByOperationId) {
      const original = (await target.store.load(target.scope))
        .find((value) => value.intent.client_operation_id === createdByOperationId);
      if (!original || original.command.kind !== "create" || original.command.entryKind !== "directory") {
        throw new Error(t("fileTree.operation.pendingParentMissing"));
      }
      const accepted = original.receipt ?? (await getWorkspaceFileOperations(port, [createdByOperationId], gatewayWorkspaceKey(target.scope), target.scope.workspaceId)).operations[0];
      if (!accepted || accepted.operation_id !== createdByOperationId || !accepted.created_path) {
        throw new Error(t("fileTree.operation.pendingParentNotAccepted"));
      }
      directoryKey = { ...original.intent.directory_key, path: accepted.created_path };
    } else {
      const cached = callbacks.current.directoryVersion(directoryPath);
      if (cached) { directoryKey = cached.key; revision = cached.revision; }
      else {
        const snapshot = await getWorkspaceFiles(port, directoryPath, gatewayWorkspaceKey(target.scope));
        directoryKey = snapshot.directory_key;
        revision = snapshot.directory_revision;
      }
    }
    if (epoch !== generation.current) return;
    if (command.kind === "copy") {
      const [sourcePath] = await resolveWorkspaceFileReferences(port, [command.sourcePath], gatewayWorkspaceKey(target.scope));
      command = { ...command, sourcePath };
    } else if (command.kind === "paste") {
      command = { ...command, sourcePaths: await resolveWorkspaceFileReferences(port, command.sourcePaths, gatewayWorkspaceKey(target.scope)) };
    }
    if (epoch !== generation.current) return;
    await target.store.insert({ scope: target.scope, directoryPath: directoryKey.path, directoryKey,
      directoryRevision: revision, command, createdByOperationId });
    if (epoch !== generation.current) return;
    callbacks.current.onStatusChange(t("fileTree.operation.savedWaitingAcceptance"));
    await run();
  }, [open, port, run]);

  return { submit, pendingCreations };
}
