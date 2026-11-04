/** 浏览器展示数据的唯一持久化入口；事务提交后才向调用方报告成功。 */
const DATABASE = "boxteam-browser-views";
const STORE = "views";
let database: Promise<IDBDatabase> | undefined;

export function openBrowserViewDatabase(): Promise<IDBDatabase> {
  if (!database) {
    database = new Promise((resolve, reject) => {
      const request = indexedDB.open(DATABASE, 1);
      request.onupgradeneeded = () => request.result.createObjectStore(STORE);
      request.onerror = () => reject(new Error(`打开浏览器视图数据库失败：${request.error?.message}`));
      request.onblocked = () => reject(new Error("浏览器视图数据库升级被其他窗口阻塞，请关闭旧窗口后重试"));
      request.onsuccess = () => {
        const db = request.result;
        db.onversionchange = () => { db.close(); database = undefined; };
        resolve(db);
      };
    });
    void database.catch(() => { database = undefined; });
  }
  return database;
}

export function browserViewKey(gatewayId: string, kind: string, ...owner: string[]): string {
  if (!gatewayId || owner.some(value => !value)) throw new Error("浏览器视图缺少固定 Gateway 或资源身份");
  return JSON.stringify([gatewayId, kind, ...owner]);
}

export async function readBrowserView<T>(key: string): Promise<T | undefined> {
  const db = await openBrowserViewDatabase();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(STORE, "readonly");
    const request = transaction.objectStore(STORE).get(key) as IDBRequest<T | undefined>;
    transaction.oncomplete = () => resolve(request.result);
    transaction.onabort = () => reject(new Error(`读取浏览器视图失败：${transaction.error?.message}`));
  });
}

/** 读改写共用一个事务，多个 Tab／窗口更新不同字段时不会丢失彼此的修改。 */
export async function updateBrowserView<T>(key: string, update: (current: T | undefined) => T | undefined): Promise<T | undefined> {
  const db = await openBrowserViewDatabase();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(STORE, "readwrite");
    const store = transaction.objectStore(STORE);
    const request = store.get(key) as IDBRequest<T | undefined>;
    let value: T | undefined;
    let failure: unknown;
    request.onsuccess = () => {
      try {
        value = update(request.result);
        if (value === undefined) store.delete(key);
        else store.put(value, key);
      } catch (error) {
        failure = error;
        transaction.abort();
      }
    };
    transaction.oncomplete = () => { publishChange(key); resolve(value); };
    transaction.onabort = () => reject(failure ?? new Error(`保存浏览器视图失败：${transaction.error?.message}`));
  });
}

/** 缓存索引只保存大小和访问顺序；正文独立存储，更新与淘汰共用同一个事务。 */
export async function updateBrowserViewCache<T extends { bytes: number; savedAt: number }>(
  key: string, indexKey: string, update: (current: T | undefined) => T | undefined,
  limits: { entries: number; bytes: number },
): Promise<void> {
  const db = await openBrowserViewDatabase();
  type Index = Record<string, { bytes: number; savedAt: number }>;
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(STORE, "readwrite");
    const store = transaction.objectStore(STORE);
    const request = store.get(key) as IDBRequest<T | undefined>;
    let failure: unknown;
    request.onsuccess = () => {
      const indexRequest = store.get(indexKey) as IDBRequest<Index | undefined>;
      indexRequest.onsuccess = () => {
        try {
          const value = update(request.result);
          const index = { ...indexRequest.result };
          if (value) {
            store.put(value, key);
            index[key] = { bytes: value.bytes, savedAt: value.savedAt };
          } else {
            store.delete(key);
            delete index[key];
          }
          let bytes = 0, count = 0;
          for (const [entryKey, entry] of Object.entries(index).sort((a, b) => b[1].savedAt - a[1].savedAt)) {
            if (count >= limits.entries || bytes + entry.bytes > limits.bytes) {
              delete index[entryKey];
              store.delete(entryKey);
            } else { bytes += entry.bytes; count++; }
          }
          store.put(index, indexKey);
        } catch (error) {
          failure = error;
          transaction.abort();
        }
      };
    };
    transaction.oncomplete = () => { publishChange(key); resolve(); };
    transaction.onabort = () => reject(failure ?? new Error(`保存浏览器缓存失败：${transaction.error?.message}`));
  });
}

const listeners = new Set<(key: string) => void>();
let channel: BroadcastChannel | undefined;

function publishChange(key: string): void {
  for (const listener of listeners) listener(key);
  channel?.postMessage(key);
}

/** 当前页面及同源窗口共享变更通知，实际值始终重新从 IndexedDB 读取。 */
export function subscribeBrowserViews(listener: (key: string) => void): () => void {
  listeners.add(listener);
  if (!channel) {
    channel = new BroadcastChannel(DATABASE);
    channel.onmessage = event => {
      if (typeof event.data !== "string") return;
      for (const listener of listeners) listener(event.data);
    };
  }
  return () => {
    listeners.delete(listener);
    if (!listeners.size) { channel?.close(); channel = undefined; }
  };
}
