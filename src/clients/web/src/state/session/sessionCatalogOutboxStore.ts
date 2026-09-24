import {
  catalogOutboxPartitionKey,
  createCatalogOutbox,
  orderedCatalogOutboxOperations,
  type CatalogOutbox,
  type CatalogOutboxOperation,
  type CatalogOutboxPartition,
} from "./sessionCatalogOutbox";

/**
 * 会话目录 outbox 的 IndexedDB 持久层（OpenSpec 8.1-H）。
 *
 * 契约要点：
 *
 * - 按稳定 `gateway/workspace/principal` 分区键隔离；同一分区内按 `client_sequence`
 *   有序写入；
 * - **本地持久化成功前不得派发后端入队**，因此写入失败必须响亮抛出，由调用方撤销
 *   pending 并显示错误；
 * - 刷新/重开时按分区键整体读回，恢复未对账的 pending 命令。
 *
 * 端口只保留 load/write/delete 三个方法，IndexedDB 是唯一实现；不预写内存/远端等
 * 其它实现，也不为「将来可能」留兼容分支。
 */

/** 可持久化形态：与 `client_sequence` 组成对象仓库主键，保证分区内有序。 */
export interface CatalogOutboxStoredOperation {
  partition_key: string;
  client_sequence: number;
  operation: CatalogOutboxOperation;
}

export interface CatalogOutboxPersistencePort {
  load(partitionKey: string): Promise<CatalogOutboxStoredOperation[]>;
  write(records: readonly CatalogOutboxStoredOperation[]): Promise<void>;
  delete(partitionKey: string, clientSequences: readonly number[]): Promise<void>;
}

export const CATALOG_OUTBOX_DATABASE_NAME = "boxteam-session-catalog-outbox";
export const CATALOG_OUTBOX_STORE_NAME = "operations";
const CATALOG_OUTBOX_SCHEMA_VERSION = 1;

function requireIndexedDb(): IDBFactory {
  if (typeof indexedDB === "undefined") {
    throw new Error(
      "当前运行环境没有 IndexedDB，无法持久化会话目录 outbox；拒绝以内存态冒充持久化",
    );
  }
  return indexedDB;
}

function requestResult<T>(request: IDBRequest<T>, context: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(
      new Error(`${context}失败: ${request.error?.message ?? "未知 IndexedDB 错误"}`),
    );
  });
}

function transactionDone(transaction: IDBTransaction, context: string): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = () => reject(
      new Error(`${context}被中止: ${transaction.error?.message ?? "未知 IndexedDB 错误"}`),
    );
    transaction.onerror = () => reject(
      new Error(`${context}失败: ${transaction.error?.message ?? "未知 IndexedDB 错误"}`),
    );
  });
}

/** 打开（并按需创建）outbox 数据库；schema 只建一个带分区索引的对象仓库。 */
export function openCatalogOutboxDatabase(): Promise<IDBDatabase> {
  const factory = requireIndexedDb();
  return new Promise<IDBDatabase>((resolve, reject) => {
    const request = factory.open(
      CATALOG_OUTBOX_DATABASE_NAME,
      CATALOG_OUTBOX_SCHEMA_VERSION,
    );
    request.onupgradeneeded = () => {
      const database = request.result;
      if (database.objectStoreNames.contains(CATALOG_OUTBOX_STORE_NAME)) return;
      const store = database.createObjectStore(CATALOG_OUTBOX_STORE_NAME, {
        keyPath: ["partition_key", "client_sequence"],
      });
      store.createIndex("partition_key", "partition_key", { unique: false });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(
      new Error(`打开会话目录 outbox 数据库失败: ${request.error?.message ?? "未知错误"}`),
    );
  });
}

/** IndexedDB 实现；连接在整个 SPA 生命周期内复用。 */
export function createIndexedDbCatalogOutboxPort(
  database: IDBDatabase,
): CatalogOutboxPersistencePort {
  return {
    async load(partitionKey) {
      const transaction = database.transaction(CATALOG_OUTBOX_STORE_NAME, "readonly");
      const store = transaction.objectStore(CATALOG_OUTBOX_STORE_NAME);
      const records = await requestResult(
        store.index("partition_key").getAll(partitionKey) as IDBRequest<
          CatalogOutboxStoredOperation[]
        >,
        "读取会话目录 outbox",
      );
      return records.sort((left, right) => left.client_sequence - right.client_sequence);
    },
    async write(records) {
      if (records.length === 0) return;
      const transaction = database.transaction(CATALOG_OUTBOX_STORE_NAME, "readwrite");
      const store = transaction.objectStore(CATALOG_OUTBOX_STORE_NAME);
      for (const record of records) store.put(record);
      await transactionDone(transaction, "写入会话目录 outbox");
    },
    async delete(partitionKey, clientSequences) {
      if (clientSequences.length === 0) return;
      const transaction = database.transaction(CATALOG_OUTBOX_STORE_NAME, "readwrite");
      const store = transaction.objectStore(CATALOG_OUTBOX_STORE_NAME);
      for (const clientSequence of clientSequences) {
        store.delete([partitionKey, clientSequence]);
      }
      await transactionDone(transaction, "清理会话目录 outbox");
    },
  };
}

// 同一分区内的写入必须串行：并发写入会让后发的 pending 命令先落盘，破坏 client_sequence
// 顺序，进而让后端看到与本地不一致的因果顺序。
const writeChainByPartition = new Map<string, Promise<void>>();

async function withPartitionWriteLock(
  partitionKey: string,
  action: () => Promise<void>,
): Promise<void> {
  const previous = writeChainByPartition.get(partitionKey) ?? Promise.resolve();
  const next = previous.then(action, action);
  writeChainByPartition.set(partitionKey, next.catch(() => undefined));
  return await next;
}

function toStoredOperation(
  partitionKey: string,
  operation: CatalogOutboxOperation,
): CatalogOutboxStoredOperation {
  return {
    partition_key: partitionKey,
    client_sequence: operation.client_sequence,
    operation,
  };
}

/**
 * 按序持久化给定 pending 命令；**持久化成功前调用方不得派发后端入队**。
 *
 * 写入失败时按原样抛出本地存储错误，绝不吞掉、绝不静默降级为内存态。
 */
export async function persistCatalogOutboxPendingOperations(
  port: CatalogOutboxPersistencePort,
  partition: CatalogOutboxPartition,
  operations: readonly CatalogOutboxOperation[],
): Promise<void> {
  const partitionKey = catalogOutboxPartitionKey(partition);
  const ordered = [...operations].sort(
    (left, right) => left.client_sequence - right.client_sequence,
  );
  await withPartitionWriteLock(partitionKey, async () => {
    await port.write(ordered.map((operation) => toStoredOperation(partitionKey, operation)));
  });
}

/** 从持久层恢复该分区的 outbox；刷新/重开后调用，未对账命令原样回来。 */
export async function loadCatalogOutbox(
  port: CatalogOutboxPersistencePort,
  partition: CatalogOutboxPartition,
): Promise<CatalogOutbox> {
  const partitionKey = catalogOutboxPartitionKey(partition);
  const records = await port.load(partitionKey);
  let outbox = createCatalogOutbox(partition);
  let maxSequence = 0;
  for (const record of records) {
    if (record.partition_key !== partitionKey) {
      throw new Error(
        `outbox 记录的分区键与请求不符: 期望 ${partitionKey}, 实际 ${record.partition_key}`,
      );
    }
    if (record.operation.client_sequence !== record.client_sequence) {
      throw new Error(
        `outbox 记录主键与 operation 序号不一致: ${record.partition_key}/${record.client_sequence}`,
      );
    }
    maxSequence = Math.max(maxSequence, record.client_sequence);
    outbox = { ...outbox, operations: [...outbox.operations, record.operation] };
  }
  outbox = { ...outbox, operations: orderedCatalogOutboxOperations(outbox) };
  return { ...outbox, next_client_sequence: maxSequence + 1 };
}

/** 只在 terminal 结果与 catalog revision 完成对账后清理对应持久条目。 */
export async function deleteCatalogOutboxOperations(
  port: CatalogOutboxPersistencePort,
  partition: CatalogOutboxPartition,
  clientSequences: readonly number[],
): Promise<void> {
  const partitionKey = catalogOutboxPartitionKey(partition);
  await withPartitionWriteLock(partitionKey, async () => {
    await port.delete(partitionKey, clientSequences);
  });
}
