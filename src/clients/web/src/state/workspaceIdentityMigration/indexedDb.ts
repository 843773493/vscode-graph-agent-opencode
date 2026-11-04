export type IdentityUpgrade = (transaction: IDBTransaction) => void;

/** 旧队列未确认归属前，不允许普通打开入口把旧项解释成新分区。 */
export function requireEmptyIdentityUpgrade(transaction: IDBTransaction, stores: string[], fail: (error: Error) => void): void {
  for (const name of stores) {
    const count = transaction.objectStore(name).count();
    count.onsuccess = () => {
      if (count.result) {
        fail(new Error(`存在旧身份的 ${name} 记录；请显式导入工作区身份恢复材料，原记录已保留`));
        transaction.abort();
      }
    };
  }
}

export interface StoredEntries { keys: IDBValidKey[]; values: unknown[] }

/** 规划与写入都在原事务事件内完成；异常中止整批，不给 IndexedDB 留异步提交窗口。 */
export function transformIdentityStores(transaction: IDBTransaction, stores: string[], apply: (entries: Map<string, StoredEntries>) => void,
  fail: (error: Error) => void): void {
  const entries = new Map<string, StoredEntries>();
  let remaining = stores.length * 2;
  const received = () => {
    if (--remaining !== 0) return;
    try { apply(entries); }
    catch (error) { fail(error instanceof Error ? error : new Error(String(error))); transaction.abort(); }
  };
  for (const name of stores) {
    const store = transaction.objectStore(name), entry: StoredEntries = { keys: [], values: [] };
    entries.set(name, entry);
    const keys = store.getAllKeys(), values = store.getAll();
    keys.onsuccess = () => { entry.keys = keys.result; received(); };
    values.onsuccess = () => { entry.values = values.result; received(); };
  }
}
