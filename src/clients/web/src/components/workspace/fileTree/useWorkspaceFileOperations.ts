import { useCallback, useEffect, useRef, useState } from "react";
import {
  createWorkspaceFileEntry,
  copyWorkspaceFileEntry,
  getWorkspace,
  getWorkspaceFileOperations,
  getWorkspaceFiles,
  pasteWorkspaceFileEntries,
  resolveWorkspaceFileReferences,
  uploadWorkspaceFileEntries,
} from "../../../api";
import { getGatewayHealth } from "../../../gatewayApi";
import { HttpRequestError } from "../../../api/http";
import { NAVIGATION_MUTATION_TERMINAL_STATES } from "../../../api/session/sessionCatalogOperations";
import type { WorkspaceDirectoryKey, WorkspaceDirectoryRevision, WorkspaceFileList, WorkspaceFileOperationReceipt } from "../../../types/backend";
import {
  openWorkspaceFileIntentStore,
  type FileIntentCommand,
  type FileIntentScope,
  type StoredFileIntent,
  type WorkspaceFileIntentStore,
} from "../../../state/workspaceFiles/intentStore";
import { errorDisplayMessage } from "../../../utils/errorMessage";

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

  const open = useCallback(async (): Promise<Driver> => {
    if (!workspaceId) throw new Error("文件操作需要固定的 Gateway 工作区身份");
    if (!driver.current) {
      driver.current = (async () => {
        const [health, workspace, rootFiles, store] = await Promise.all([
          getGatewayHealth(port), getWorkspace(port, workspaceId), getWorkspaceFiles(port, "", workspaceId),
          openWorkspaceFileIntentStore(),
        ]);
        if (!health.gateway_id || !workspace.workspace_id || !rootFiles.directory_key.root
          || rootFiles.directory_key.workspace_id !== workspace.workspace_id) {
          throw new Error("缺少固定 Gateway/工作区身份，拒绝受理文件操作");
        }
        return { store, scope: { gatewayId: health.gateway_id, gatewayWorkspaceId: workspaceId,
          backendWorkspaceId: workspace.workspace_id, workspaceRoot: rootFiles.directory_key.root, actor: "local" } };
      })();
    }
    return await driver.current;
  }, [port, workspaceId]);

  const run = useCallback((): Promise<void> => {
    if (pump.current) return pump.current;
    const epoch = generation.current;
    const current = () => epoch === generation.current;
    const task = (async () => {
      const target = await open();
      if (!current()) return;
      const records = await target.store.load(target.scope);
      const oldOwners = (await target.store.loadForWorkspace(target.scope.gatewayWorkspaceId))
        .filter((record) => record.scopeKey !== records[0]?.scopeKey
          && (record.scope.gatewayId !== target.scope.gatewayId
            || record.scope.backendWorkspaceId !== target.scope.backendWorkspaceId
            || record.scope.workspaceRoot !== target.scope.workspaceRoot));
      if (oldOwners.length > 0) callbacks.current.onError("存在原 Gateway/工作区的未确认文件操作，保留原目标等待恢复");
      if (records.length === 0) { setPendingCreations([]); return; }
      // 查询与重试前重新核固定 route，Gateway 或工作区重连不能改投原 intent。
      const [health, workspace, rootFiles] = await Promise.all([
        getGatewayHealth(port), getWorkspace(port, target.scope.gatewayWorkspaceId),
        getWorkspaceFiles(port, "", target.scope.gatewayWorkspaceId),
      ]);
      if (!current()) return;
      if (health.gateway_id !== target.scope.gatewayId
        || workspace.workspace_id !== target.scope.backendWorkspaceId
        || rootFiles.directory_key.workspace_id !== target.scope.backendWorkspaceId
        || rootFiles.directory_key.root !== target.scope.workspaceRoot) {
        throw new Error("文件意图原 Gateway/工作区已变化，保留原请求等待原目标恢复");
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
        try {
          const status = await getWorkspaceFileOperations(port, [record.intent.client_operation_id], target.scope.gatewayWorkspaceId, target.scope.backendWorkspaceId);
          if (!current()) return;
          if (status.worker_state === "failed") throw new Error(`文件 worker 已停止: ${status.worker_error}`);
          const found = status.operations.find((value) => value.operation_id === record.intent.client_operation_id);
          if (!found) throw new Error("原文件操作查询缺少对应回执");
          receipt = found;
        } catch (error) {
          if (!current()) return;
          if (!(error instanceof HttpRequestError) || error.status !== 404) throw error;
          // 只有原 ID 明确不存在才重发；Blob 与 intent 均来自同一持久记录。
          try {
            const command = record.command;
            switch (command.kind) {
              case "create": receipt = await createWorkspaceFileEntry(port, record.directoryPath,
                { name: command.name, kind: command.entryKind, intent: record.intent }, target.scope.gatewayWorkspaceId); break;
              case "copy": receipt = await copyWorkspaceFileEntry(port, record.directoryPath,
                { source_path: command.sourcePath, source_scope: command.sourceScope, intent: record.intent }, target.scope.gatewayWorkspaceId); break;
              case "paste": receipt = await pasteWorkspaceFileEntries(port, record.directoryPath,
                { source_paths: command.sourcePaths, intent: record.intent }, target.scope.gatewayWorkspaceId); break;
              case "upload": receipt = await uploadWorkspaceFileEntries(port, record.directoryPath,
                command.entries, record.intent, target.scope.gatewayWorkspaceId); break;
            }
          } catch (sendError) {
            if (!current()) return;
            // 4xx 也先查询原 ID：并发 tab 可能已受理，不能删除未知结果。
            try {
              const status = await getWorkspaceFileOperations(port, [record.intent.client_operation_id], target.scope.gatewayWorkspaceId, target.scope.backendWorkspaceId);
              const found = status.operations.find((value) => value.operation_id === record.intent.client_operation_id);
              if (!found) throw new Error("重试查询缺少原文件操作");
              receipt = found;
            } catch (queryError) {
              if (!current()) return;
              if (sendError instanceof HttpRequestError && sendError.status >= 400 && sendError.status < 500
                && queryError instanceof HttpRequestError && queryError.status === 404) {
                await callbacks.current.reconcileDirectories([], record.directoryPath);
                if (!current()) return;
                await target.store.remove(record);
              }
              throw new AggregateError([sendError, queryError], "文件受理失败或结果未知，保留原身份核实");
            }
          }
        }
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
            callbacks.current.onStatusChange("文件操作已完成");
          }
          else callbacks.current.onError(`文件操作失败（效果: ${receipt.effect}）: ${receipt.error_detail ?? receipt.state}`);
        } else {
          callbacks.current.onStatusChange(`文件操作已受理: ${receipt.state === "queued" ? "排队中" : "执行中"}`);
        }
        } catch (error) {
          blocked.add(record.intent.client_operation_id);
          if (current()) callbacks.current.onError(errorDisplayMessage(error));
        }
      }
      updatePending(await target.store.load(target.scope));
    })();
    pump.current = task;
    void task.finally(() => { if (pump.current === task) pump.current = null; }).catch(() => undefined);
    return task;
  }, [open, port]);

  useEffect(() => {
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
        if ((await store.loadForWorkspace(workspaceId)).length > 0) await run();
      }
      catch (error) { if (!stopped) callbacks.current.onError(errorDisplayMessage(error)); }
      if (!stopped) timer = setTimeout(() => void poll(), 500);
    };
    void poll();
    return () => { stopped = true; generation.current += 1; clearTimeout(timer); };
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
        throw new Error("原待创建目录不存在，请从后端文件树重新选择目标");
      }
      const accepted = original.receipt ?? (await getWorkspaceFileOperations(port, [createdByOperationId], target.scope.gatewayWorkspaceId, target.scope.backendWorkspaceId)).operations[0];
      if (!accepted || accepted.operation_id !== createdByOperationId || !accepted.created_path) {
        throw new Error("原目录尚未受理，请等待受理后再创建后继");
      }
      directoryKey = { ...original.intent.directory_key, path: accepted.created_path };
    } else {
      const cached = callbacks.current.directoryVersion(directoryPath);
      if (cached) { directoryKey = cached.key; revision = cached.revision; }
      else {
        const snapshot = await getWorkspaceFiles(port, directoryPath, target.scope.gatewayWorkspaceId);
        directoryKey = snapshot.directory_key;
        revision = snapshot.directory_revision;
      }
    }
    if (epoch !== generation.current) return;
    if (command.kind === "copy") {
      const [sourcePath] = await resolveWorkspaceFileReferences(port, [command.sourcePath], target.scope.gatewayWorkspaceId);
      command = { ...command, sourcePath };
    } else if (command.kind === "paste") {
      command = { ...command, sourcePaths: await resolveWorkspaceFileReferences(port, command.sourcePaths, target.scope.gatewayWorkspaceId) };
    }
    if (epoch !== generation.current) return;
    await target.store.insert({ scope: target.scope, directoryPath: directoryKey.path, directoryKey,
      directoryRevision: revision, command, createdByOperationId });
    if (epoch !== generation.current) return;
    callbacks.current.onStatusChange("文件操作已保存，等待受理");
    await run();
  }, [open, port, run]);

  return { submit, pendingCreations };
}
