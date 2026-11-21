import type { SessionCatalogOperationReceipt } from "../../api/session/sessionCatalogOperations";
import type { IdentityUpgrade } from "../workspaceIdentityMigration/indexedDb";
import { LOCAL_DATABASES, LocalDatabaseError, openBrowserDatabase } from "../browserPersistence";
import {
  applyCatalogOutboxOperationReceipt,
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
  applyReceipts(
    partitionKey: string,
    receipts: readonly SessionCatalogOperationReceipt[],
  ): Promise<CatalogOutboxStoredOperation[]>;
  delete(partitionKey: string, clientOperationIds: readonly string[]): Promise<void>;
}

export const CATALOG_OUTBOX_DATABASE_NAME = LOCAL_DATABASES.catalog.name;
export const CATALOG_OUTBOX_STORE_NAME = "operations";
const CATALOG_OUTBOX_SEQUENCE_STORE_NAME = "partition-sequences";
const OPERATION_ID_INDEX = "partition_operation_id";
const SEQUENCE_FLOOR_KEY = "sequence-only-upgrade";

interface CatalogOutboxSequenceRecord {
  partition_key: string;
  next_client_sequence: number;
}

/** 没有旧命令时无需推断身份；保留原计数器，用冻结下限避免改名后的分区重新从 1 编号。 */
function upgradeSequenceOnlyOutbox(transaction: IDBTransaction, fail: (error: Error) => void): void {
  const count = transaction.objectStore(CATALOG_OUTBOX_STORE_NAME).count();
  count.onsuccess = () => {
    if (count.result) {
      fail(new LocalDatabaseError(transaction.db.name, "identity", new Error("存在旧身份的 operations 记录，需完成工作区身份升级；原记录已保留")));
      transaction.abort();
      return;
    }
    let floor = 1;
    const cursor = transaction.objectStore(CATALOG_OUTBOX_SEQUENCE_STORE_NAME).openCursor();
    cursor.onsuccess = () => {
      if (!cursor.result) {
        transaction.objectStore("identity-recovery").put({ migration_id: SEQUENCE_FLOOR_KEY, next_client_sequence: floor });
        return;
      }
      const { next_client_sequence: next } = cursor.result.value as CatalogOutboxSequenceRecord;
      if (!Number.isSafeInteger(next) || next < 1) {
        fail(new Error("会话目录 outbox 序号高水位无效，原记录已保留"));
        transaction.abort();
        return;
      }
      floor = Math.max(floor, next);
      cursor.result.continue();
    };
  };
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

export function openCatalogOutboxDatabase(upgrade?: IdentityUpgrade): Promise<IDBDatabase> {
  return openBrowserDatabase(LOCAL_DATABASES.catalog, (database, transaction, oldVersion, fail) => {
    if (oldVersion > 0 && oldVersion < 2) {
      fail(new Error("不支持的目录 outbox v1，原记录已保留，不能清空旧命令"));
      transaction.abort();
      return;
    }
    if (oldVersion === 2) {
      if (upgrade) upgrade(transaction);
      else upgradeSequenceOnlyOutbox(transaction, fail);
    }
    const operations = database.objectStoreNames.contains(CATALOG_OUTBOX_STORE_NAME)
      ? transaction.objectStore(CATALOG_OUTBOX_STORE_NAME)
      : database.createObjectStore(CATALOG_OUTBOX_STORE_NAME, { keyPath: ["partition_key", "client_sequence"] });
    if (!operations.indexNames.contains("partition_key")) operations.createIndex("partition_key", "partition_key", { unique: false });
    if (!operations.indexNames.contains(OPERATION_ID_INDEX)) operations.createIndex(OPERATION_ID_INDEX, ["partition_key", "operation.client_operation_id"], { unique: true });
    if (!database.objectStoreNames.contains(CATALOG_OUTBOX_SEQUENCE_STORE_NAME)) database.createObjectStore(CATALOG_OUTBOX_SEQUENCE_STORE_NAME, { keyPath: "partition_key" });
    database.createObjectStore("identity-recovery", { keyPath: "migration_id" });
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
      // IDB 按索引键、再按复合主键排序；同分区内主键第二项就是 client_sequence。
      return records;
    },
    async insert(partitionKey, operation) {
      if (operation.client_sequence !== null) {
        throw new Error("新 outbox operation 的 client_sequence 必须由 IndexedDB 分配");
      }
      if (operation.state !== "pending_local") {
        throw new Error("只有 pending_local operation 可以首次写入 outbox");
      }

      const transaction = database.transaction(
        [CATALOG_OUTBOX_STORE_NAME, CATALOG_OUTBOX_SEQUENCE_STORE_NAME, "identity-recovery"],
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
      const floorRequest = transaction.objectStore("identity-recovery").get(SEQUENCE_FLOOR_KEY) as IDBRequest<
        Pick<CatalogOutboxSequenceRecord, "next_client_sequence"> | undefined
      >;
      floorRequest.onsuccess = () => {
        const highWater = highWaterRequest.result;
        const sequence = Math.max(highWater?.next_client_sequence ?? 1, floorRequest.result?.next_client_sequence ?? 1);
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
    async applyReceipts(partitionKey, receipts) {
      if (receipts.length === 0) return [];
      const byOperation = new Map<string, SessionCatalogOperationReceipt[]>();
      for (const receipt of receipts) {
        const versions = byOperation.get(receipt.operation_id) ?? [];
        versions.push(receipt);
        byOperation.set(receipt.operation_id, versions);
      }
      const transaction = database.transaction(CATALOG_OUTBOX_STORE_NAME, "readwrite");
      const done = transactionDone(transaction, "持久合并会话目录回执");
      const store = transaction.objectStore(CATALOG_OUTBOX_STORE_NAME);
      const operationIds = store.index(OPERATION_ID_INDEX);
      const records: CatalogOutboxStoredOperation[] = [];
      let conflict: Error | null = null;
      for (const [operationId, versions] of byOperation) {
        const request = operationIds.get([partitionKey, operationId]) as IDBRequest<
          CatalogOutboxStoredOperation | undefined
        >;
        request.onsuccess = () => {
          const existing = request.result;
          // 其它 tab 已完成清理的命令不得由迟到回执重新插入或重新分配序号。
          if (!existing) return;
          try {
            let operation: CatalogOutboxOperation = existing.operation;
            for (const receipt of versions) {
              operation = applyCatalogOutboxOperationReceipt(operation, receipt);
            }
            const updated: CatalogOutboxStoredOperation = {
              ...existing,
              operation: { ...operation, client_sequence: existing.client_sequence },
            };
            if (operation !== existing.operation) store.put(updated);
            records.push(updated);
          } catch (error: unknown) {
            conflict = error instanceof Error ? error : new Error(String(error));
            transaction.abort();
          }
        };
      }
      try {
        await done;
      } catch (error: unknown) {
        if (conflict) throw conflict;
        throw error;
      }
      return records.sort((left, right) => left.client_sequence - right.client_sequence);
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
  return catalogOutboxFromRecords(partition, records);
}

/** 回执事务从磁盘当前版本推进；提交后才把镜像交给 driver 发布。 */
export async function persistCatalogOutboxReceipts(
  port: CatalogOutboxPersistencePort,
  partition: CatalogOutboxPartition,
  receipts: readonly SessionCatalogOperationReceipt[],
): Promise<CatalogOutbox> {
  const records = await port.applyReceipts(catalogOutboxPartitionKey(partition), receipts);
  return catalogOutboxFromRecords(partition, records);
}

function catalogOutboxFromRecords(
  partition: CatalogOutboxPartition,
  records: readonly CatalogOutboxStoredOperation[],
): CatalogOutbox {
  const partitionKey = catalogOutboxPartitionKey(partition);
  const outbox = createCatalogOutbox(partition);
  const operations = outbox.operations;
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
    operations.push(record.operation);
  }
  return normalizeRestoredCatalogOutbox({
    ...outbox,
    operations: orderedCatalogOutboxOperations(outbox),
  });
}

/** 只清理 operation，不清理分区高水位。 */
export async function deleteCatalogOutboxOperations(
  port: CatalogOutboxPersistencePort,
  partition: CatalogOutboxPartition,
  clientOperationIds: readonly string[],
): Promise<void> {
  await port.delete(catalogOutboxPartitionKey(partition), clientOperationIds);
}
