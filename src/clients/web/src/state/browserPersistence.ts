import { ReportedError } from "../utils/errorMessage";

export const LOCAL_DATABASES = {
  views: { name: "boxteam-browser-views", version: 1, label: "localData.views" },
  catalog: { name: "boxteam-session-catalog-outbox", version: 3, label: "localData.catalog" },
  files: { name: "boxteam-workspace-file-intents", version: 2, label: "localData.files" },
} as const;

export class LocalDatabaseError extends Error {
  constructor(readonly database: string, readonly kind: "identity" | "blocked" | "open", cause: unknown) {
    super(cause instanceof Error ? cause.message : String(cause), { cause });
    this.name = "LocalDatabaseError";
  }
}

export function localDatabaseError(error: unknown): LocalDatabaseError | undefined {
  if (error instanceof LocalDatabaseError) return error;
  return error instanceof ReportedError ? localDatabaseError(error.cause) : undefined;
}

const failures = new Map<string, LocalDatabaseError>();
const blocked = new Set<string>();
const blockedMessage = "数据库升级被旧窗口阻塞，请关闭旧窗口后重试；原记录已保留";
let reporter: ((error: LocalDatabaseError) => void) | undefined;

/** 只有宿主已经提供可见出口，才交付 ReportedError；独立组件仍收到原异常。 */
export function setLocalDatabaseReporter(value: (error: LocalDatabaseError) => void): () => void {
  reporter = value;
  for (const error of failures.values()) value(error);
  return () => { if (reporter === value) reporter = undefined; };
}

export function localDatabaseFailure(database: string): LocalDatabaseError | undefined { return failures.get(database); }

type Upgrade = (database: IDBDatabase, transaction: IDBTransaction, oldVersion: number, fail: (error: Error) => void) => void;

/** 只统一连接生命周期；各 owner 在唯一 versionchange 事务里定义自己的 schema。 */
export function openBrowserDatabase(definition: { name: string; version?: number }, upgrade?: Upgrade): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    let failure: Error | undefined, abandoned = false;
    const rejectOpen = (cause: unknown, kind: LocalDatabaseError["kind"] = "open") => {
      const error = cause instanceof LocalDatabaseError ? cause : new LocalDatabaseError(definition.name, kind, cause);
      if (definition.version !== undefined) {
        failures.set(definition.name, error);
        if (reporter) {
          try { reporter(error); }
          catch (reportError) { reject(new AggregateError([error, reportError], "本地数据库故障通知失败")); return; }
          reject(new ReportedError(error.message, error)); return;
        }
      }
      reject(error);
    };
    if (typeof indexedDB === "undefined") { rejectOpen(new Error("当前环境缺少 IndexedDB，无法保存本地数据")); return; }
    // 原升级请求结束前，新请求也只会排队挂起；此时明确返回同一阻塞原因。
    if (definition.version !== undefined && blocked.has(definition.name)) { rejectOpen(new Error(blockedMessage), "blocked"); return; }
    let request: IDBOpenDBRequest;
    try { request = definition.version === undefined ? indexedDB.open(definition.name) : indexedDB.open(definition.name, definition.version); }
    catch (error) { rejectOpen(error); return; }
    request.onupgradeneeded = event => {
      const transaction = request.transaction!;
      if (abandoned) { transaction.abort(); return; }
      try {
        if (!upgrade) throw new Error("诊断期间数据库已被移除；没有创建新数据库");
        upgrade(request.result, transaction, event.oldVersion, error => { failure = error; });
      } catch (error) {
        failure = error instanceof Error ? error : new Error(String(error));
        transaction.abort();
      }
    };
    request.onblocked = () => {
      abandoned = true;
      blocked.add(definition.name);
      rejectOpen(new Error(blockedMessage), "blocked");
    };
    request.onerror = () => {
      if (abandoned) blocked.delete(definition.name);
      else rejectOpen(failure ?? request.error);
    };
    request.onsuccess = () => {
      const database = request.result;
      if (abandoned) { blocked.delete(definition.name); database.close(); return; }
      database.onversionchange = () => database.close();
      if (definition.version !== undefined) failures.delete(definition.name);
      resolve(database);
    };
  });
}

/** 用户主动诊断时才读取计数；不升级数据库，也不读取草稿、历史正文或上传 Blob。 */
export async function inspectLocalDatabases() {
  const existing = await indexedDB.databases();
  return Promise.all(Object.values(LOCAL_DATABASES).map(async definition => {
    const metadata = existing.find(item => item.name === definition.name);
    const result = { ...definition, storedVersion: metadata?.version, stores: {} as Record<string, number>, error: localDatabaseFailure(definition.name)?.message };
    if (!metadata || blocked.has(definition.name)) return result;
    let database: IDBDatabase | undefined;
    try {
      database = await openBrowserDatabase({ name: definition.name });
      const names = Array.from(database.objectStoreNames);
      if (!names.length) return result;
      const transaction = database.transaction(names, "readonly");
      await new Promise<void>((resolve, reject) => {
        for (const name of names) {
          const count = transaction.objectStore(name).count();
          count.onsuccess = () => { result.stores[name] = count.result; };
        }
        transaction.oncomplete = () => resolve();
        transaction.onabort = () => reject(transaction.error ?? new Error("读取本地数据计数被中止"));
      });
      return result;
    } catch (error) {
      result.error = error instanceof Error ? error.message : String(error);
      return result;
    } finally { database?.close(); }
  }));
}
