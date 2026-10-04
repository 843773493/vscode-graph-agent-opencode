import {
  catalogOutboxPartitionKey,
  createCatalogOutbox,
  normalizeRestoredCatalogOutbox,
  orderedCatalogOutboxOperations,
  type CatalogOutbox,
  type CatalogOutboxOperation,
  type CatalogOutboxPartition,
} from "./sessionCatalogOutbox";

/** 唯一 IndexedDB 持久层：分区序号分配与 operation 插入原子提交。 */
export interface CatalogOutboxStoredOperation {
  partition_key: string;
  client_sequence: number;
  operation: Omit<CatalogOutboxOperation, "client_sequence"> & {
    client_sequence: number;
  };
}

export interface CatalogOutboxPersistencePort {
  load(partitionKey: string): Promise<CatalogOutboxStoredOperation[]>;
  insert(
    partitionKey: string,
    operation: CatalogOutboxOperation,
  ): Promise<CatalogOutboxStoredOperation>;
  delete(partitionKey: string, clientOperationIds: readonly string[]): Promise<void>;
}

export const CATALOG_OUTBOX_DATABASE_NAME = "boxteam-session-catalog-outbox";
export const CATALOG_OUTBOX_STORE_NAME = "operations";
const CATALOG_OUTBOX_SEQUENCE_STORE_NAME = "partition-sequences";
const CATALOG_OUTBOX_SCHEMA_VERSION = 2;
const OPERATION_ID_INDEX = "partition_operation_id";

interface CatalogOutboxSequenceRecord {
  partition_key: string;
  next_client_sequence: number;
}

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
      new Error(context + "失败: " + (request.error?.message ?? "未知 IndexedDB 错误")),
    );
  });
}

function transactionDone(transaction: IDBTransaction, context: string): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = () => reject(
      new Error(context + "被中止: " + (transaction.error?.message ?? "未知 IndexedDB 错误")),
    );
    transaction.onerror = () => reject(
      new Error(context + "失败: " + (transaction.error?.message ?? "未知 IndexedDB 错误")),
    );
  });
}

function configureSchema(request: IDBOpenDBRequest): void {
  request.onupgradeneeded = (event) => {
    const database = request.result;
    const transaction = request.transaction;
    if (!transaction) throw new Error("IndexedDB schema upgrade 缺少 versionchange transaction");

    if (event.oldVersion > 0 && event.oldVersion < 2) {
      // v1 仅是未发布的开发结构，升级时清空其中的 pending，避免旧序号与新高水位并存。
      database.deleteObjectStore(CATALOG_OUTBOX_STORE_NAME);
    }
    const operations = database.objectStoreNames.contains(CATALOG_OUTBOX_STORE_NAME)
      ? transaction.objectStore(CATALOG_OUTBOX_STORE_NAME)
      : database.createObjectStore(CATALOG_OUTBOX_STORE_NAME, {
        keyPath: ["partition_key", "client_sequence"],
      });
    if (!operations.indexNames.contains("partition_key")) {
      operations.createIndex("partition_key", "partition_key", { unique: false });
    }
    if (!operations.indexNames.contains(OPERATION_ID_INDEX)) {
      operations.createIndex(
        OPERATION_ID_INDEX,
        ["partition_key", "operation.client_operation_id"],
        { unique: true },
      );
    }

    if (!database.objectStoreNames.contains(CATALOG_OUTBOX_SEQUENCE_STORE_NAME)) {
      database.createObjectStore(CATALOG_OUTBOX_SEQUENCE_STORE_NAME, {
        keyPath: "partition_key",
      });
    }
  };
}

export function openCatalogOutboxDatabase(): Promise<IDBDatabase> {
  const factory = requireIndexedDb();
  return new Promise<IDBDatabase>((resolve, reject) => {
    const request = factory.open(CATALOG_OUTBOX_DATABASE_NAME, CATALOG_OUTBOX_SCHEMA_VERSION);
    configureSchema(request);
    request.onsuccess = () => {
      request.result.onversionchange = () => request.result.close();
      resolve(request.result);
    };
    request.onerror = () => reject(
      new Error("打开会话目录 outbox 数据库失败: " + (request.error?.message ?? "未知错误")),
    );
  });
}

/** IndexedDB 实现；连接在当前页面生命周期内复用。 */
export function createIndexedDbCatalogOutboxPort(
  database: IDBDatabase,
): CatalogOutboxPersistencePort {
  return {
    async load(partitionKey) {
      const transaction = database.transaction(CATALOG_OUTBOX_STORE_NAME, "readonly");
      const done = transactionDone(transaction, "读取会话目录 outbox");
      const records = await requestResult(
        transaction.objectStore(CATALOG_OUTBOX_STORE_NAME)
          .index("partition_key")
          .getAll(partitionKey) as IDBRequest<CatalogOutboxStoredOperation[]>,
        "读取会话目录 outbox",
      );
      await done;
      return records.sort((left, right) => left.client_sequence - right.client_sequence);
    },
    async insert(partitionKey, operation) {
      if (operation.client_sequence !== null) {
        throw new Error("新 outbox operation 的 client_sequence 必须由 IndexedDB 分配");
      }
      if (operation.state !== "pending_local") {
        throw new Error("只有 pending_local operation 可以首次写入 outbox");
      }

      const transaction = database.transaction(
        [CATALOG_OUTBOX_STORE_NAME, CATALOG_OUTBOX_SEQUENCE_STORE_NAME],
        "readwrite",
      );
      const done = transactionDone(transaction, "原子分配并写入会话目录 outbox");
      const operations = transaction.objectStore(CATALOG_OUTBOX_STORE_NAME);
      const sequences = transaction.objectStore(CATALOG_OUTBOX_SEQUENCE_STORE_NAME);
      let stored: CatalogOutboxStoredOperation | null = null;
      let conflict: Error | null = null;
      const highWaterRequest = sequences.get(partitionKey) as IDBRequest<
        CatalogOutboxSequenceRecord | undefined
      >;
      highWaterRequest.onsuccess = () => {
        const highWater = highWaterRequest.result;
        const sequence = highWater?.next_client_sequence ?? 1;
        if (!Number.isSafeInteger(sequence) || sequence < 1
          || sequence === Number.MAX_SAFE_INTEGER) {
          conflict = new Error("会话目录 outbox 序号高水位无效: " + sequence);
          transaction.abort();
          return;
        }

        const idRequest = operations.index(OPERATION_ID_INDEX).getKey([
          partitionKey,
          operation.client_operation_id,
        ]) as IDBRequest<IDBValidKey | undefined>;
        idRequest.onsuccess = () => {
          if (idRequest.result !== undefined) {
            conflict = new Error(
            "会话目录 outbox 已存在同 operation ID，原记录保持不变: "
            + operation.client_operation_id,
            );
            transaction.abort();
            return;
          }

          stored = {
            partition_key: partitionKey,
            client_sequence: sequence,
            operation: { ...operation, client_sequence: sequence },
          };
          // add 拒绝相同主键；唯一 ID 索引拒绝同分区 ID 的另一条命令。
          operations.add(stored);
          sequences.put({
            partition_key: partitionKey,
            next_client_sequence: sequence + 1,
          } satisfies CatalogOutboxSequenceRecord);
        };
      };
      try {
        await done;
      } catch (error: unknown) {
        if (conflict) throw conflict;
        throw error;
      }
      if (conflict) throw conflict;
      if (!stored) throw new Error("IndexedDB 事务完成但没有写入 operation");
      return stored;
    },
    async delete(partitionKey, clientOperationIds) {
      if (clientOperationIds.length === 0) return;
      const transaction = database.transaction(CATALOG_OUTBOX_STORE_NAME, "readwrite");
      const done = transactionDone(transaction, "清理会话目录 outbox");
      const store = transaction.objectStore(CATALOG_OUTBOX_STORE_NAME);
      const operationIds = store.index(OPERATION_ID_INDEX);
      for (const operationId of clientOperationIds) {
        const request = operationIds.getKey([partitionKey, operationId]);
        request.onsuccess = () => {
          if (request.result !== undefined) store.delete(request.result);
        };
      }
      await done;
    },
  };
}

/** 返回事务分配的 canonical operation；在此之前调用方不得派发后端入队。 */
export async function persistCatalogOutboxPendingOperation(
  port: CatalogOutboxPersistencePort,
  partition: CatalogOutboxPartition,
  operation: CatalogOutboxOperation,
): Promise<CatalogOutboxStoredOperation> {
  return await port.insert(catalogOutboxPartitionKey(partition), operation);
}

export async function loadCatalogOutbox(
  port: CatalogOutboxPersistencePort,
  partition: CatalogOutboxPartition,
): Promise<CatalogOutbox> {
  const partitionKey = catalogOutboxPartitionKey(partition);
  const records = await port.load(partitionKey);
  let outbox = createCatalogOutbox(partition);
  const operationIds = new Set<string>();
  const sequences = new Set<number>();
  for (const record of records) {
    if (record.partition_key !== partitionKey) {
      throw new Error(
        "outbox 记录的分区键与请求不符: 期望 " + partitionKey + ", 实际 " + record.partition_key,
      );
    }
    if (record.operation.client_sequence !== record.client_sequence) {
      throw new Error(
        "outbox 记录主键与 operation 序号不一致: "
        + record.partition_key + "/" + record.client_sequence,
      );
    }
    if (!Number.isSafeInteger(record.client_sequence) || record.client_sequence < 1) {
      throw new Error("outbox 记录 client_sequence 无效: " + record.client_sequence);
    }
    if (operationIds.has(record.operation.client_operation_id)) {
      throw new Error("outbox 存在重复 operation ID: " + record.operation.client_operation_id);
    }
    if (sequences.has(record.client_sequence)) {
      throw new Error("outbox 存在重复 client_sequence: " + record.client_sequence);
    }
    operationIds.add(record.operation.client_operation_id);
    sequences.add(record.client_sequence);
    outbox = { ...outbox, operations: [...outbox.operations, record.operation] };
  }
  outbox = { ...outbox, operations: orderedCatalogOutboxOperations(outbox) };
  return normalizeRestoredCatalogOutbox(outbox);
}

/** 只清理 operation，不清理分区高水位。 */
export async function deleteCatalogOutboxOperations(
  port: CatalogOutboxPersistencePort,
  partition: CatalogOutboxPartition,
  clientOperationIds: readonly string[],
): Promise<void> {
  await port.delete(catalogOutboxPartitionKey(partition), clientOperationIds);
}
