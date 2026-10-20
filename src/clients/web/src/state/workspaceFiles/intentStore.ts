import type {
  WorkspaceDirectoryKey,
  WorkspaceFileMutationIntent,
  WorkspaceFileOperationReceipt,
} from "../../types/backend";
import type { WorkspaceUploadEntry } from "../../api/workspaceFilesystem";

export interface FileIntentScope {
  gatewayId: string;
  gatewayWorkspaceId: string;
  backendWorkspaceId: string;
  workspaceRoot: string;
  actor: "local";
}

export type FileIntentCommand =
  | { kind: "create"; name: string; entryKind: "file" | "directory" }
  | { kind: "copy"; sourcePath: string; sourceScope: "workspace" | "filesystem" }
  | { kind: "paste"; sourcePaths: string[] }
  | { kind: "upload"; entries: WorkspaceUploadEntry[] };

export interface StoredFileIntent {
  scopeKey: string;
  scope: FileIntentScope;
  directoryPath: string;
  intent: WorkspaceFileMutationIntent;
  command: FileIntentCommand;
  receipt: WorkspaceFileOperationReceipt | null;
}

export interface NewFileIntent {
  scope: FileIntentScope;
  directoryPath: string;
  directoryKey: WorkspaceDirectoryKey;
  directoryRevision: number | null;
  command: FileIntentCommand;
  createdByOperationId?: string;
  relativeChildPath?: string;
}

const DATABASE_NAME = "boxteam-workspace-file-intents";
const RECORDS = "intents";
const SEQUENCES = "sequences";

export function fileIntentScopeKey(scope: FileIntentScope): string {
  for (const value of Object.values(scope)) {
    if (!value || value.includes("\u0000")) throw new Error("文件意图的固定目标身份无效");
  }
  if (!scope.workspaceRoot.startsWith("boxteam://")) throw new Error("文件意图的资源根必须使用 URI");
  return [scope.gatewayId, scope.gatewayWorkspaceId, scope.backendWorkspaceId,
    scope.workspaceRoot, scope.actor].join("\u0000");
}

function complete(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = transaction.onerror = () => reject(
      new Error(`文件意图 IndexedDB 事务失败: ${transaction.error?.message ?? "已中止"}`),
    );
  });
}

function result<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new Error(`文件意图存储失败: ${request.error?.message}`));
  });
}

function sameDirectory(left: WorkspaceDirectoryKey, right: WorkspaceDirectoryKey): boolean {
  return left.workspace_id === right.workspace_id && directoryPath(left) === directoryPath(right);
}

function directoryPath(key: WorkspaceDirectoryKey): string {
  return key.path;
}

function overlappingPath(a: string, b: string): boolean {
  return a === b || a.startsWith(`${b}/`) || b.startsWith(`${a}/`);
}

function sources(command: FileIntentCommand): string[] {
  if (command.kind === "paste") return command.sourcePaths;
  if (command.kind === "copy") return [command.sourcePath];
  return [];
}

function validateReferences(directory: string, key: WorkspaceDirectoryKey, command: FileIntentCommand): void {
  if (directory !== key.path || [directory, key.root, ...sources(command)].some((uri) => !uri.startsWith("boxteam://"))) {
    throw new Error("文件意图的位置必须先由目标 owner 解析为资源 URI");
  }
}

function conflictsWith(value: StoredFileIntent, input: NewFileIntent): boolean {
  const before = directoryPath(value.intent.directory_key);
  const after = directoryPath(input.directoryKey);
  return overlappingPath(before, after)
    || sources(input.command).some((path) => overlappingPath(before, path))
    || sources(value.command).some((path) => overlappingPath(after, path));
}

/** 只保存未确认意图；分区序号、依赖与上传 Blob 在一个事务内冻结。 */
export class WorkspaceFileIntentStore {
  constructor(private readonly database: IDBDatabase) {}

  async loadForWorkspace(gatewayWorkspaceId: string): Promise<StoredFileIntent[]> {
    const transaction = this.database.transaction(RECORDS, "readonly");
    const done = complete(transaction);
    const records = await result(transaction.objectStore(RECORDS).index("gatewayWorkspaceId")
      .getAll(gatewayWorkspaceId) as IDBRequest<StoredFileIntent[]>);
    await done;
    return records;
  }

  async load(scope: FileIntentScope): Promise<StoredFileIntent[]> {
    const scopeKey = fileIntentScopeKey(scope);
    const transaction = this.database.transaction(RECORDS, "readonly");
    const done = complete(transaction);
    const values = await result(
      transaction.objectStore(RECORDS).index("scopeKey").getAll(scopeKey) as IDBRequest<StoredFileIntent[]>,
    );
    await done;
    for (const value of values) {
      validateReferences(value.directoryPath, value.intent.directory_key, value.command);
      if (value.scopeKey !== fileIntentScopeKey(value.scope)
        || value.scopeKey !== scopeKey
        || value.intent.directory_key.workspace_id !== scope.backendWorkspaceId) {
        throw new Error("文件意图存储的原目标身份不一致，拒绝改投");
      }
    }
    return values.sort((a, b) => a.intent.client_sequence - b.intent.client_sequence);
  }

  async insert(input: NewFileIntent): Promise<StoredFileIntent> {
    validateReferences(input.directoryPath, input.directoryKey, input.command);
    const scopeKey = fileIntentScopeKey(input.scope);
    if (input.directoryKey.workspace_id !== input.scope.backendWorkspaceId) {
      throw new Error("目录键与固定后端工作区不一致");
    }
    const transaction = this.database.transaction([RECORDS, SEQUENCES], "readwrite");
    const done = complete(transaction);
    const records = transaction.objectStore(RECORDS);
    const sequences = transaction.objectStore(SEQUENCES);
    let stored: StoredFileIntent | null = null;
    let invalid: Error | null = null;
    const existing = records.index("scopeKey").getAll(scopeKey) as IDBRequest<StoredFileIntent[]>;
    existing.onsuccess = () => {
      const high = sequences.get(scopeKey) as IDBRequest<{ scopeKey: string; next: number } | undefined>;
      high.onsuccess = () => {
        const sequence = high.result?.next ?? 1;
        if (!Number.isSafeInteger(sequence) || sequence < 1 || sequence === Number.MAX_SAFE_INTEGER) {
          invalid = new Error("文件意图序号高水位无效");
          transaction.abort();
          return;
        }
        const previous = existing.result.sort((a, b) => b.intent.client_sequence - a.intent.client_sequence);
        const same = previous.find((value) => sameDirectory(value.intent.directory_key, input.directoryKey));
        const related = previous.filter((value) => conflictsWith(value, input));
        // 已有依赖链只保留最前沿，不能在同目录连续写时无界堆积全部历史 ID。
        const dependencies = related.filter((value) => !related.some((other) => (
          other.intent.depends_on.includes(value.intent.client_operation_id)
        ))).map((value) => value.intent.client_operation_id);
        if (input.createdByOperationId) dependencies.push(input.createdByOperationId);
        const revisionFrom = input.createdByOperationId ?? same?.intent.client_operation_id ?? null;
        if (revisionFrom) dependencies.push(revisionFrom);
        if (new Set(dependencies).size > 100) {
          invalid = new Error("文件操作关联的未确认前序超过 100 项，请等待前序收口后再操作");
          transaction.abort();
          return;
        }
        if (!revisionFrom && input.directoryRevision === null) {
          invalid = new Error("文件意图缺少原目录 revision");
          transaction.abort();
          return;
        }
        stored = {
          scopeKey, scope: input.scope, directoryPath: input.directoryPath, command: input.command, receipt: null,
          intent: {
            client_operation_id: `op_${crypto.randomUUID().replace(/-/g, "")}`,
            client_sequence: sequence,
            directory_key: input.directoryKey,
            expected_directory_revision: revisionFrom ? null : input.directoryRevision,
            directory_revision_from_operation_id: revisionFrom,
            depends_on: [...new Set(dependencies)],
            created_by_operation_id: input.createdByOperationId ?? null,
            relative_child_path: input.relativeChildPath ?? "",
            client_ref: `file_${crypto.randomUUID()}`,
          },
        };
        records.add(stored);
        sequences.put({ scopeKey, next: sequence + 1 });
      };
    };
    await done.catch((error: unknown) => { throw invalid ?? error; });
    if (!stored) throw new Error("文件意图事务没有保存原请求");
    return stored;
  }

  async accept(record: StoredFileIntent, receipt: WorkspaceFileOperationReceipt): Promise<void> {
    if (receipt.operation_id !== record.intent.client_operation_id
      || receipt.client_sequence !== record.intent.client_sequence) throw new Error("文件受理回执身份不一致");
    const transaction = this.database.transaction(RECORDS, "readwrite");
    const done = complete(transaction);
    const store = transaction.objectStore(RECORDS);
    const read = store.get([record.scopeKey, record.intent.client_operation_id]) as IDBRequest<StoredFileIntent | undefined>;
    read.onsuccess = () => {
      const current = read.result;
      if (current && (!current.receipt || current.receipt.receipt_revision < receipt.receipt_revision)) {
        store.put({ ...current, receipt });
      }
    };
    await done;
  }

  async remove(record: StoredFileIntent): Promise<void> {
    const transaction = this.database.transaction(RECORDS, "readwrite");
    const done = complete(transaction);
    transaction.objectStore(RECORDS).delete([record.scopeKey, record.intent.client_operation_id]);
    await done;
  }
}

let opening: Promise<WorkspaceFileIntentStore> | null = null;

export function openWorkspaceFileIntentStore(): Promise<WorkspaceFileIntentStore> {
  if (opening) return opening;
  if (typeof indexedDB === "undefined") return Promise.reject(new Error("缺少 IndexedDB，不能持久受理文件操作"));
  opening = new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE_NAME, 1);
    request.onupgradeneeded = () => {
      const records = request.result.createObjectStore(RECORDS, { keyPath: ["scopeKey", "intent.client_operation_id"] });
      records.createIndex("scopeKey", "scopeKey");
      records.createIndex("gatewayWorkspaceId", "scope.gatewayWorkspaceId");
      request.result.createObjectStore(SEQUENCES, { keyPath: "scopeKey" });
    };
    request.onerror = () => { opening = null; reject(new Error(`打开文件意图存储失败: ${request.error?.message}`)); };
    request.onsuccess = () => {
      request.result.onversionchange = () => { request.result.close(); opening = null; };
      resolve(new WorkspaceFileIntentStore(request.result));
    };
  });
  return opening;
}
