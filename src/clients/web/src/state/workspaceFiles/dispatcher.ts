import {
  copyWorkspaceFileEntry,
  createWorkspaceFileEntry,
  getWorkspaceFileOperations,
  pasteWorkspaceFileEntries,
  uploadWorkspaceFileEntries,
} from "../../api/workspaceFilesystem";
import { HttpRequestError } from "../../api/http";
import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import type { WorkspaceFileOperationReceipt } from "../../types/backend";
import type { StoredFileIntent, WorkspaceFileIntentStore } from "./intentStore";
import { NAVIGATION_MUTATION_TERMINAL_STATES } from "../../api/session/sessionCatalogOperations";

/** 服务明确拒绝且原 ID 查无记录；调用方可以安全释放本地意图。 */
export class WorkspaceFileIntentRejectedError extends AggregateError {
  constructor(sendError: unknown, queryError: unknown) {
    super([sendError, queryError], "文件操作未受理，原 operation ID 查无记录");
    this.name = "WorkspaceFileIntentRejectedError";
  }
}

async function queryWorkspaceFileReceipt(port: number, record: StoredFileIntent, operationId: string,
  signal?: AbortSignal): Promise<WorkspaceFileOperationReceipt> {
  signal?.throwIfAborted();
  const status = await getWorkspaceFileOperations(port, [operationId],
    gatewayWorkspaceKey(record.scope), record.scope.workspaceId);
  signal?.throwIfAborted();
  if (status.worker_state === "failed") throw new Error(`文件 worker 已停止: ${status.worker_error}`);
  const receipt = status.operations.find(value => value.operation_id === operationId);
  if (!receipt) throw new Error("原文件操作查询缺少对应回执，保留本地 intent");
  return receipt;
}

async function findWorkspaceFileReceipt(port: number, record: StoredFileIntent,
  signal?: AbortSignal): Promise<WorkspaceFileOperationReceipt | undefined> {
  try {
    return await queryWorkspaceFileReceipt(port, record, record.intent.client_operation_id, signal);
  } catch (error) {
    signal?.throwIfAborted();
    if (!(error instanceof HttpRequestError) || error.status !== 404) throw error;
  }
}

async function sendWorkspaceFileIntent(port: number, record: StoredFileIntent,
  signal?: AbortSignal): Promise<WorkspaceFileOperationReceipt> {
  signal?.throwIfAborted();
  const workspaceId = gatewayWorkspaceKey(record.scope);
  try {
    const command = record.command;
    switch (command.kind) {
      case "create":
        return await createWorkspaceFileEntry(port, record.directoryPath,
          { name: command.name, kind: command.entryKind, intent: record.intent }, workspaceId);
      case "copy":
        return await copyWorkspaceFileEntry(port, record.directoryPath,
          { source_path: command.sourcePath, source_scope: command.sourceScope, intent: record.intent }, workspaceId);
      case "paste":
        return await pasteWorkspaceFileEntries(port, record.directoryPath,
          { source_paths: command.sourcePaths, intent: record.intent }, workspaceId);
      case "upload":
        return await uploadWorkspaceFileEntries(port, record.directoryPath,
          command.entries, record.intent, workspaceId);
    }
  } catch (sendError) {
    signal?.throwIfAborted();
    try {
      return await queryWorkspaceFileReceipt(port, record, record.intent.client_operation_id, signal);
    } catch (queryError) {
      signal?.throwIfAborted();
      if (sendError instanceof HttpRequestError && sendError.status >= 400 && sendError.status < 500
        && queryError instanceof HttpRequestError && queryError.status === 404) {
        throw new WorkspaceFileIntentRejectedError(sendError, queryError);
      }
      throw new AggregateError([sendError, queryError], "文件操作受理结果未知，保留原意图等待核实");
    }
  }
}

/** 文件树只等受理；终态等待与发送复用同一查询和原意图提交链。 */
export async function dispatchWorkspaceFileIntent(port: number, record: StoredFileIntent, signal?: AbortSignal): Promise<WorkspaceFileOperationReceipt> {
  return await findWorkspaceFileReceipt(port, record, signal) ?? sendWorkspaceFileIntent(port, record, signal);
}

export function isWorkspaceFileOperationTerminal(receipt: WorkspaceFileOperationReceipt): boolean {
  return NAVIGATION_MUTATION_TERMINAL_STATES.includes(receipt.state);
}

async function waitForRelatedReceipt(port: number, record: StoredFileIntent, operationId: string,
  signal?: AbortSignal): Promise<WorkspaceFileOperationReceipt> {
  while (true) {
    const receipt = await queryWorkspaceFileReceipt(port, record, operationId, signal);
    if (isWorkspaceFileOperationTerminal(receipt)) return receipt;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
}

export async function waitForWorkspaceFileOperation(port: number, record: StoredFileIntent,
  store: WorkspaceFileIntentStore, onReceipt?: (receipt: WorkspaceFileOperationReceipt) => void,
  signal?: AbortSignal): Promise<WorkspaceFileOperationReceipt> {
  // 后台已受理的操作由自身权威回执收口；旧依赖失败不能阻挡其终态恢复。
  let receipt = await findWorkspaceFileReceipt(port, record, signal);
  if (!receipt) {
    const pending = await store.load(record.scope);
    for (const dependency of record.intent.depends_on) {
      signal?.throwIfAborted();
      const previous = pending.find(value => value.intent.client_operation_id === dependency);
      const related = previous
        ? await waitForWorkspaceFileOperation(port, previous, store, onReceipt, signal)
        : await waitForRelatedReceipt(port, record, dependency, signal);
      if (related.state !== "committed") throw new Error(`前序文件操作未提交，后继保持等待: ${related.error_detail ?? related.state}`);
    }
    receipt = await sendWorkspaceFileIntent(port, record, signal);
  }
  signal?.throwIfAborted();
  await store.accept(record, receipt);
  onReceipt?.(receipt);
  while (!isWorkspaceFileOperationTerminal(receipt)) {
    signal?.throwIfAborted();
    await new Promise(resolve => setTimeout(resolve, 500));
    signal?.throwIfAborted();
    const updated = await queryWorkspaceFileReceipt(port, record, record.intent.client_operation_id, signal);
    if (updated.receipt_revision > receipt.receipt_revision) {
      receipt = updated;
      await store.accept(record, receipt);
      onReceipt?.(receipt);
    }
  }
  return receipt;
}
